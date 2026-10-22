"""Six specified model families; no target labels enter graph messages."""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F
from torch_geometric.nn import GATConv, GCNConv


class Homogeneous(nn.Module):
    def __init__(self, inputs, hidden, kind, **_kwargs):
        super().__init__()
        self.kind = kind
        if kind == "GCN":
            self.first = GCNConv(inputs, hidden)
            self.second = GCNConv(hidden, hidden)
        else:
            self.first = GATConv(inputs, hidden // 2, heads=2, edge_dim=1, dropout=0.1)
            self.second = GATConv(hidden, hidden, heads=1, edge_dim=1, dropout=0.1)
        self.classifier = nn.Linear(hidden, 1)

    def forward(self, g, explain=False):
        x, edges, weight = g["x"], g["edge_index"], g["edge_weight"]
        edge_arg = weight if self.kind == "GCN" else weight[:, None]
        h = F.relu(self.first(x, edges, edge_arg))
        h = F.dropout(h, p=0.15, training=self.training)
        h = F.relu(self.second(h, edges, edge_arg))
        if explain and h.requires_grad:
            h.retain_grad()
        return self.classifier(h).squeeze(-1), h


class Heterogeneous(nn.Module):
    def __init__(self, inputs, hidden, kind="HGNNs", types=5, relations=6):
        super().__init__()
        self.hidden = hidden
        self.kind = kind
        self.maps = nn.ModuleList([nn.Linear(inputs, hidden) for _ in range(types)])
        self.messages = nn.ModuleList([nn.Linear(hidden, hidden, bias=False) for _ in range(relations)])
        self.relation_score = nn.ModuleList([nn.Linear(hidden * 2, 1) for _ in range(relations)])
        self.risk_attention = nn.Linear(hidden, hidden)
        self.fusion = nn.Sequential(nn.Linear(hidden * 2, hidden), nn.ReLU())
        self.classifier = nn.Linear(hidden, 1)

    def forward(self, g, explain=False, return_messages=False):
        x = g["x"]
        node_type = g["node_type"]
        own = x.new_zeros((len(x), self.hidden))
        for t, mapping in enumerate(self.maps):
            mask = node_type == t
            own[mask] = F.relu(mapping(x[mask]))
        aggregates = []
        scores = []
        available = []
        message_records = []
        for rel, transform in enumerate(self.messages):
            mask = g["edge_type"] == rel
            source, target = g["edge_index"][:, mask]
            weights = g["edge_weight"][mask]
            denominator = x.new_zeros(len(x))
            denominator.index_add_(0, target, weights)
            normal = weights / denominator[target].clamp_min(1e-8)
            messages = transform(own[source]) * normal[:, None]
            aggregate = x.new_zeros((len(x), self.hidden))
            aggregate.index_add_(0, target, messages)
            score = self.relation_score[rel](torch.cat([own, aggregate], dim=-1)).squeeze(-1)
            aggregates.append(aggregate)
            scores.append(score)
            available.append(denominator > 0)
            if return_messages:
                message_records.append((source, target, messages, rel))
        present = torch.stack(available, dim=1)
        logits = torch.stack(scores, dim=1).masked_fill(~present, -1e4)
        attention = torch.softmax(logits, dim=1) * present
        attention = attention / attention.sum(1, keepdim=True).clamp_min(1e-8)
        structure = (torch.stack(aggregates, dim=1) * attention[:, :, None]).sum(1)
        dimension = torch.sigmoid(self.risk_attention(structure))
        structure = structure * dimension
        hidden = self.fusion(torch.cat([own, structure], dim=-1))
        if explain and hidden.requires_grad:
            hidden.retain_grad()
        result = self.classifier(F.dropout(hidden, p=0.15, training=self.training)).squeeze(-1)
        if return_messages:
            return result, hidden, message_records, attention, dimension
        return result, hidden


class Temporal(nn.Module):
    def __init__(self, embedding, hidden):
        super().__init__()
        self.gru = nn.GRU(embedding + 1, hidden, batch_first=True)
        self.classifier = nn.Linear(hidden, 1)

    def forward(self, sequence):
        _, h = self.gru(sequence)
        return self.classifier(h[-1]).squeeze(-1)


def make(kind, inputs, hidden):
    return Homogeneous(inputs, hidden, kind) if kind in ("GCN", "GAT") else Heterogeneous(inputs, hidden, kind)
