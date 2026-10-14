"""序列深度模型（需要 PyTorch）：LSTM、GRU、TCN、PatchTST 风格的 Transformer。

输入是每个样本（时刻 t、标的 n）过去 seq_len 根 K 线的特征序列 ``[seq_len, F]``，只含 t 及以前的数据；
输出是一个数（回归）或一个概率（分类）。训练用 AdamW，按验证集损失早停，固定随机种子、单线程以便复现。
"""

from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
import torch
from torch import nn

from .learners import Learner


class _RNN(nn.Module):
    def __init__(self, cell: str, F: int, p: dict[str, Any]) -> None:
        super().__init__()
        cls = nn.LSTM if cell == "lstm" else nn.GRU
        self.rnn = cls(
            F, p["hidden"], num_layers=p["layers"], batch_first=True, dropout=p["dropout"] if p["layers"] > 1 else 0.0
        )
        self.head = nn.Sequential(nn.Dropout(p["dropout"]), nn.Linear(p["hidden"], 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.rnn(x)
        return self.head(out[:, -1]).squeeze(-1)


class _TCN(nn.Module):
    def __init__(self, F: int, p: dict[str, Any]) -> None:
        super().__init__()
        layers: list[nn.Module] = []
        ch = F
        for i in range(p["layers"]):
            d = 2**i
            layers += [
                nn.ConstantPad1d((2 * d, 0), 0.0),
                nn.Conv1d(ch, p["hidden"], 3, dilation=d),
                nn.ReLU(),
                nn.Dropout(p["dropout"]),
            ]
            ch = p["hidden"]
        self.net = nn.Sequential(*layers)
        self.head = nn.Linear(ch, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.net(x.transpose(1, 2))  # [B, C, L]，因果卷积只看左边
        return self.head(h[:, :, -1]).squeeze(-1)


class _PatchTransformer(nn.Module):
    def __init__(self, F: int, p: dict[str, Any]) -> None:
        super().__init__()
        self.patch = max(1, min(int(p["patch"]), int(p["seq_len"])))
        n_patch = int(p["seq_len"]) // self.patch
        self.proj = nn.Linear(F * self.patch, p["d_model"])
        self.pos = nn.Parameter(torch.zeros(1, n_patch, p["d_model"]))
        heads = max(1, min(int(p["heads"]), p["d_model"]))
        while p["d_model"] % heads:
            heads -= 1
        layer = nn.TransformerEncoderLayer(
            p["d_model"], heads, dim_feedforward=p["d_model"] * 2, dropout=p["dropout"], batch_first=True
        )
        self.enc = nn.TransformerEncoder(layer, num_layers=p["layers"])
        self.head = nn.Linear(p["d_model"], 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, L, F = x.shape
        n = L // self.patch
        x = x[:, L - n * self.patch :].reshape(B, n, self.patch * F)
        h = self.enc(self.proj(x) + self.pos[:, :n])
        return self.head(h.mean(dim=1)).squeeze(-1)


def _build(kind: str, F: int, p: dict[str, Any]) -> nn.Module:
    if kind in ("lstm", "gru"):
        return _RNN(kind, F, p)
    if kind == "tcn":
        return _TCN(F, p)
    return _PatchTransformer(F, p)


class SeqLearner(Learner):
    def _seq(self, ds: Any, idx: np.ndarray) -> torch.Tensor:
        return torch.as_tensor(ds.seq_X(idx, int(self.params["seq_len"])), dtype=torch.float32)

    def fit(self, ds, tr, va, *, stop=None, init=None):
        torch.manual_seed(self.seed)
        torch.set_num_threads(2)
        p = self.params
        X, y = self._seq(ds, tr), torch.as_tensor(ds.y_rows[tr], dtype=torch.float32)
        Xv, yv = self._seq(ds, va), torch.as_tensor(ds.y_rows[va], dtype=torch.float32)
        self.F = X.shape[2]
        self.net = _build(self.kind, self.F, p)
        if init and init.get("weights"):
            try:
                self._load_weights(init["weights"])
            except Exception:  # 结构变了就从头训练
                pass
        opt = torch.optim.AdamW(self.net.parameters(), lr=p["lr"], weight_decay=1e-4)
        loss_fn = nn.BCEWithLogitsLoss() if self.task == "clf" else nn.MSELoss()
        g = torch.Generator().manual_seed(self.seed)
        best, best_state, bad, curve = float("inf"), None, 0, []
        for ep in range(1, int(p["epochs"]) + 1):
            if stop:
                stop()
            self.net.train()
            order = torch.randperm(len(y), generator=g)
            tot = 0.0
            for lo in range(0, len(y), int(p["batch"])):
                bi = order[lo : lo + int(p["batch"])]
                opt.zero_grad()
                loss = loss_fn(self.net(X[bi]), y[bi])
                loss.backward()
                nn.utils.clip_grad_norm_(self.net.parameters(), 1.0)
                opt.step()
                tot += float(loss.detach()) * len(bi)
            self.net.eval()
            with torch.no_grad():
                vl = float(loss_fn(self.net(Xv), yv)) if len(yv) else tot / max(len(y), 1)
            curve.append({"epoch": ep, "train": tot / max(len(y), 1), "val": vl if len(yv) else None})
            if vl < best - 1e-7:
                best, bad = vl, 0
                best_state = {k: v.detach().clone() for k, v in self.net.state_dict().items()}
            else:
                bad += 1
                if bad >= int(p["patience"]):
                    break
        if best_state is not None:
            self.net.load_state_dict(best_state)
        return curve

    def predict(self, ds, idx):
        self.net.eval()
        out = []
        with torch.no_grad():
            for lo in range(0, len(idx), 4096):
                z = self.net(self._seq(ds, idx[lo : lo + 4096]))
                out.append(torch.sigmoid(z) if self.task == "clf" else z)
        return torch.cat(out).numpy().astype(float) if out else np.zeros(0)

    def _load_weights(self, b64: str) -> None:
        buf = io.BytesIO(base64.b64decode(b64))
        self.net.load_state_dict(torch.load(buf, weights_only=True))

    def state(self):
        buf = io.BytesIO()
        torch.save(self.net.state_dict(), buf)
        return {**super().state(), "F": self.F, "weights": base64.b64encode(buf.getvalue()).decode()}

    def load(self, state):
        self.F = int(state["F"])
        self.net = _build(self.kind, self.F, self.params)
        self._load_weights(state["weights"])
