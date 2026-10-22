"""Source-specific ingestion; never treats missing labels as healthy."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from .io import LEGAL, csv_rows, listing_code, normal_name, num, save_frame, save_json, stock_code, valid_name

FINANCE = {
    "FS_Combas": {
        "A001000000": "assets",
        "A002000000": "liabilities",
        "A003000000": "equity",
        "A001100000": "current_assets",
        "A002100000": "current_liabilities",
        "A001101000": "cash",
        "A001111000": "receivables",
        "A001123000": "inventory",
    },
    "FS_Comins": {
        "B001101000": "revenue",
        "B001201000": "cost",
        "B002000000": "profit",
        "B001000000": "pretax_profit",
        "B001211101": "interest",
    },
    "FS_Comscfd": {"C001000000": "ocf"},
}


def extract_name(value):
    text = str(value).strip()
    if text in ["", "无", "无。", "--", "null", "名称变更"]:
        return ""
    match = re.search(r"企业名称[:：]\s*([^;；\n]+)", text)
    if match:
        text = match[1]
    text = normal_name(text)
    return text if 4 <= len(text) <= 100 and LEGAL.search(text) and not re.search(r"[:：;；\n]", text) else ""


def prepare(ctx, inputs, params):
    manifest = __import__("json").loads(ctx.file(params["manifest"]).read_text(encoding="utf-8"))
    files = manifest["files"]
    audit = {
        "source_files": manifest.get("provenance", []),
        "policies": {
            "finance": "12-31, consolidated A; prefer uncorrected version; no DeclareDate availability assumption",
            "trade": "consolidated 1, ranks1-5, identified names or reliable IDs; anonymous excluded",
            "status": "year-end ST reconstructed from effective-name events; listing statuses kept separate",
            "features": "lagged financial values; exact-year business metadata; no business-state feature",
            "scope": "retrospective risk-state recognition; not point-in-time historical credit forecasting",
        },
    }
    financial = None
    for table, columns in FINANCE.items():
        ctx.log("读取年度合并财务：" + table)
        keep = []
        needed = ["Stkcd", "ShortName", "Accper", "Typrep", "IfCorrect", "DeclareDate", *columns]
        for chunk in pd.read_csv(
            ctx.file(files[table]),
            encoding="utf-8-sig",
            usecols=needed,
            dtype=str,
            keep_default_na=False,
            chunksize=100_000,
        ):
            selected = chunk[(chunk.Typrep == "A") & chunk.Accper.str.endswith("-12-31")].copy()
            selected["year"] = selected.Accper.str[:4].astype(int)
            selected = selected[selected.year.between(2010, 2024)]
            keep.append(selected)
        data = pd.concat(keep, ignore_index=True).sort_values(["Stkcd", "year", "IfCorrect", "DeclareDate"])
        duplicate_count = int(data.duplicated(["Stkcd", "year"]).sum())
        data = data.drop_duplicates(["Stkcd", "year"], keep="first")
        data = data.rename(columns={"Stkcd": "stock", "ShortName": "short_name", **columns})
        for column in columns.values():
            data[column] = pd.to_numeric(data[column], errors="coerce")
        audit[table] = {
            "annual_rows": len(data),
            "duplicate_annual_keys": duplicate_count,
            "corrected_rows": int((data.IfCorrect == "1").sum()),
        }
        selected_columns = ["stock", "year", *columns.values()]
        if financial is None:
            financial = data[[*selected_columns, "short_name", "IfCorrect"]]
        else:
            financial = financial.merge(
                data[selected_columns], on=["stock", "year"], how="inner", validate="one_to_one"
            )
    financial = financial[np.isfinite(financial.revenue) & (financial.assets > 0)].copy()
    financial = financial.sort_values(["stock", "year"]).reset_index(drop=True)
    financial["icr"] = np.where(
        financial.interest > 0, (financial.pretax_profit + financial.interest) / financial.interest, np.nan
    )

    name_events = defaultdict(list)
    listing_events = defaultdict(list)
    for row in csv_rows(ctx.file(files["status_events"])):
        target = name_events if row["ChangedItem"] == "证券简称" else listing_events
        target[row["Symbol"]].append(row)
    for events in [name_events, listing_events]:
        for values in events.values():
            values.sort(key=lambda r: (r["ChangeDate"], r["DeclareDate"]))

    def st_status(stock, year, fallback):
        records = name_events.get(stock, [])
        state = records[0]["ValueBefore"] if records else fallback
        for event in records:
            if event["ChangeDate"] <= f"{year}-12-31":
                state = event["ValueAfter"]
            else:
                break
        return int(bool(re.search(r"\*?ST", state.upper())))

    financial["st_label"] = [
        st_status(s, y, n) for s, y, n in zip(financial.stock, financial.year, financial.short_name, strict=True)
    ]
    financial["shortname_st"] = financial.short_name.str.upper().str.contains("ST", regex=False).astype(int)
    audit["status_name_disagreement"] = int((financial.st_label != financial.shortname_st).sum())
    financial["composite_label"] = np.where(
        financial.st_label.eq(1), 1, np.where(np.isfinite(financial.icr), (financial.icr < 1).astype(int), -1)
    )
    financial["entity"] = "L:" + financial.stock
    financial["node_type"] = np.where(
        financial.stock.str.startswith(("002", "003", "300", "301", "688", "689", "43", "83", "87", "92")), 1, 0
    )
    finance_ref = save_frame(ctx, financial, "annual_financial")

    ctx.log("读取工商快照、实名交易与企业股权")
    enterprise = pd.DataFrame(list(csv_rows(ctx.file(files["SC_EnterpriseInfo"]), audit)))
    enterprise["year"] = enterprise.EndDate.str[:4].astype(int)
    enterprise["stock"] = enterprise.BusinessSymbol.map(listing_code)
    institution_entities = {str(row.InstitutionID): "L:" + row.stock for row in enterprise.itertuples() if row.stock}
    enterprise["entity"] = np.where(enterprise.stock.ne(""), "L:" + enterprise.stock, "I:" + enterprise.InstitutionID)
    enterprise["entity"] = [
        institution_entities.get(str(i), e) for i, e in zip(enterprise.InstitutionID, enterprise.entity, strict=True)
    ]
    enterprise["normalized_name"] = enterprise.InstitutionName.map(normal_name)
    duplicate = int(enterprise.duplicated(["entity", "year"]).sum())
    enterprise = enterprise.sort_values("EndDate").drop_duplicates(["entity", "year"], keep="last")
    audit["business_duplicate_entity_years"] = duplicate
    audit["business_years"] = {str(k): int(v) for k, v in enterprise.year.value_counts().sort_index().items()}
    enterprise_ref = save_frame(ctx, enterprise, "enterprise_snapshots")

    name_to_ids = defaultdict(set)
    names = {}
    for row in enterprise.itertuples():
        if valid_name(row.InstitutionName):
            name_to_ids[row.normalized_name].add(row.entity)
            names[row.entity] = row.InstitutionName
    trades = []
    excluded = Counter()
    for table, direction, amount in [
        ("SC_TopFiveSaleInfo", "sale", "SalesAmount"),
        ("SC_TopFivePurchaseInfo", "purchase", "PurchaseAmount"),
    ]:
        for row in csv_rows(ctx.file(files[table]), audit):
            if row["StateTypeCode"] != "1":
                excluded["parent_company_rows"] += 1
                continue
            if num(row["Rank"]) not in range(1, 6):
                excluded["totals_or_rank"] += 1
                continue
            name = normal_name(row["InstitutionName"])
            code = listing_code(row["BusinessSymbol"])
            reliable_id = row["BusinessInstitutionID"].strip()
            if code:
                entity = "L:" + code
            elif reliable_id:
                entity = institution_entities.get(reliable_id, "I:" + reliable_id)
            elif valid_name(name):
                candidates = name_to_ids.get(name, set())
                entity = next(iter(candidates)) if len(candidates) == 1 else "N:" + name
            else:
                excluded["anonymous_or_empty"] += 1
                continue
            if not stock_code(row["Symbol"]):
                excluded["invalid_anchor"] += 1
                continue
            share = num(row["ProportionOfTotalValue"]) / 100
            if not np.isfinite(share) or share <= 0 or share > 1:
                excluded["missing_invalid_weight"] += 1
                continue
            year = int(row["EndDate"][:4])
            if not 2012 <= year <= 2024:
                continue
            names.setdefault(entity, name)
            trades.append(
                {
                    "year": year,
                    "anchor": "L:" + row["Symbol"],
                    "counterparty": entity,
                    "name": name,
                    "direction": direction,
                    "weight": share,
                    "amount": num(row[amount]),
                    "source": table,
                    "rank": row["Rank"],
                }
            )
    trades = pd.DataFrame(trades).drop_duplicates(["year", "anchor", "counterparty", "direction", "rank"])
    audit["trade_filtering"] = dict(excluded)
    audit["retained_trade_rows"] = len(trades)
    trade_ref = save_frame(ctx, trades, "identified_transactions")

    capital = []
    for table in sorted(k for k in files if k.startswith("major_shareholder_")):
        for row in csv_rows(ctx.file(files[table]), audit):
            code = stock_code(row["SCode"])
            if not code or not row["Date"].endswith("-12-31"):
                continue
            raw = normal_name(row["ShrName"])
            legal = re.match(r"(.+?(?:股份有限公司|有限责任公司|有限公司|集团公司))[-－]", raw)
            name = legal[1] if legal else raw
            if (
                not valid_name(name)
                or not LEGAL.search(name)
                or any(
                    x in name
                    for x in ["证券投资基金", "全国社保基金", "香港中央结算", "中央结算(代理人)", "证券金融股份"]
                )
            ):
                excluded["non_corporate_or_custodial_shareholder"] += 1
                continue
            weight = num(row["ShrRt"]) / 100
            if not 0 < weight <= 1:
                continue
            ids = name_to_ids.get(name, set())
            entity = next(iter(ids)) if len(ids) == 1 else "N:" + name
            names.setdefault(entity, name)
            capital.append(
                {
                    "year": int(row["Date"][:4]),
                    "source": entity,
                    "target": "L:" + code,
                    "weight": weight,
                    "provenance": table,
                    "name": raw,
                }
            )
    payment_keys = {}
    for row in csv_rows(ctx.file(files["SC_ShareholderInfo"]), audit):
        if row["ShareholderTypeCode"] != "1":
            continue
        name = normal_name(row["ShareholderName"])
        if not valid_name(name) or not LEGAL.search(name):
            continue
        year = int(row["EndDate"][:4])
        weight = num(row["Proportion"]) / 100
        if not 0 < weight <= 1:
            continue
        ids = name_to_ids.get(name, set())
        source = next(iter(ids)) if len(ids) == 1 else "N:" + name
        code = listing_code(row["BusinessSymbol"])
        target = "L:" + code if code else institution_entities.get(row["InstitutionID"], "I:" + row["InstitutionID"])
        key = (year, source, target)
        old = payment_keys.get(key)
        if old is None or weight > old["weight"]:
            payment_keys[key] = {
                "year": year,
                "source": source,
                "target": target,
                "weight": weight,
                "provenance": "SC_ShareholderInfo",
                "name": name,
            }
        names.setdefault(source, name)
    capital.extend(payment_keys.values())
    capital = pd.DataFrame(capital)
    capital = capital[capital.year.between(2012, 2024)]
    capital = capital.sort_values("weight").drop_duplicates(["year", "source", "target"], keep="last")
    capital_ref = save_frame(ctx, capital, "corporate_capital")
    audit["capital_retained_edges"] = len(capital)
    audit["capital_payment_rows_deduplicated"] = len(payment_keys)

    aliases = []
    for row in csv_rows(ctx.file(files["SC_EnterpriseInfoChg"]), audit):
        if row["ChangeItem"] != "公司名称变更":
            continue
        before, after = extract_name(row["ContentBefore"]), extract_name(row["ContentAfter"])
        if not before or not after or before == after:
            continue
        code = listing_code(row.get("BusinessSymbol", ""))
        entity = "L:" + code if code else institution_entities.get(row["InstitutionID"], "I:" + row["InstitutionID"])
        aliases.append(
            {
                "entity": entity,
                "group_id": "I:" + row["InstitutionID"],
                "alias": before,
                "canonical": after,
                "effective_date": row["ChangeDate"],
                "evidence": "name_change_record",
            }
        )
    aliases_ref = save_json(ctx, aliases, "verified_aliases.json")
    name_ref = save_json(ctx, names, "entity_names.json")
    audit["alias_pairs"] = len(aliases)
    audit["multi_symbol_rows_resolved"] = int(
        ((enterprise.BusinessSymbol.str.len() > 6) & enterprise.stock.ne("")).sum()
    )
    audit["institution_to_listed_mappings"] = len(institution_entities)
    audit["annual_financial_rows"] = len(financial)
    audit["labels_by_year"] = {
        str(y): {
            "rows": len(g),
            "st_positive": int(g.st_label.sum()),
            "composite_observed": int(g.composite_label.ge(0).sum()),
        }
        for y, g in financial[financial.year.ge(2012)].groupby("year")
    }
    audit_ref = save_json(ctx, audit, "preparation_audit.json")
    ctx.log(
        f"年度财务{len(financial)}行，实名交易{len(trades)}行，法人股权{len(capital)}行，真实更名对{len(aliases)}组"
    )
    return {
        "prepared": {
            "financial": finance_ref,
            "enterprise": enterprise_ref,
            "trades": trade_ref,
            "capital": capital_ref,
            "aliases": aliases_ref,
            "names": name_ref,
            "audit": audit_ref,
        },
        "audit": audit,
    }
