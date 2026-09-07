"""ICTHub notifications styled after Tianchi's light blue and violet ranking page."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from beauclaw.core import utcnow
from beauclaw.providers import get_provider


def render_tianchi_email(payload: dict) -> tuple[str, str, str]:
    test, preview = bool(payload.get("test")), bool(payload.get("preview"))
    competition = str(payload.get("competition_name") or "天池大赛").replace("\r", " ").replace("\n", " ")[:160]
    schedule = str(payload.get("schedule_name") or "排行榜").replace("\r", " ").replace("\n", " ")[:80]
    title = "前十名榜单" if test else "榜一变化"
    subject = f"[ICTHub] 天池榜单测试 · {competition}" if test else f"[ICTHub · 关键邮件 · 榜一变动] {competition} · {schedule} · 快照 #{payload['poll_id']}"
    url = get_provider("tianchi").url(str(payload["competition_id"]))
    observed = datetime.fromisoformat(payload.get("captured_at") or utcnow()).astimezone(timezone(timedelta(hours=8)))
    stamp = f"{observed:%Y-%m-%d %H:%M:%S}（北京时间）"
    note = "样式预览 · 以下均为示例数据" if preview else "测试邮件 · 当前公开榜单" if test else "关键邮件 · 榜首变化快照永久保留"
    rows = sorted(payload.get("top10") or [], key=lambda row: row["rank"])[:10]
    plain = ["ICTHub", title, note, f"比赛：{competition}", f"阿里云天池 · {schedule}", f"快照时间：{stamp}", ""]
    changes = []

    def shown_score(entry: dict) -> str:
        return str(entry.get("display_score") or entry["score"])

    def organization(entry: dict) -> str:
        return str(entry.get("organization") or "—")

    if not test:
        for event in payload["events"]:
            before, after = event["before"], event["after"]
            old_score, new_score = shown_score(before), shown_score(after)
            # If rounding hides an actual increase, show the original score in the
            # change card. The top-ten table keeps the website's displayed precision.
            if old_score == new_score and before["score"] != after["score"]:
                old_score, new_score = str(before["score"]), str(after["score"])
            plain.extend([f"变化前：{before['name']} | {organization(before)} | 分数 {old_score}",
                          f"变化后：{after['name']} | {organization(after)} | 分数 {new_score}",
                          f"原始分数：{before['score']} → {after['score']}",
                          f"变动前快照：#{event['details'].get('previous_poll_id')}", ""])
            changes.append(f'''<tr><td style="padding:26px 24px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="table-layout:fixed"><tr>
<td width="44%" valign="top" style="padding:18px 14px;background:#f7f8fa;border:1px solid #e9ebf0;border-radius:8px;overflow-wrap:anywhere;word-wrap:break-word;word-break:break-word">
<div style="font-size:12px;color:#777e90;margin-bottom:10px">变化前</div>
<div style="font-size:16px;font-weight:700;color:#353b4a">{escape(str(before['name']))}</div>
<div style="font-size:12px;color:#81889a;line-height:1.6;margin-top:5px">{escape(organization(before))}</div>
<div style="font-size:26px;font-weight:700;color:#61697c;line-height:1.35;margin-top:14px">{escape(old_score)}</div><div style="font-size:11px;color:#8d94a4">分数</div></td>
<td width="12%" align="center" style="font-size:24px;color:#6472fc">→</td>
<td width="44%" valign="top" style="padding:18px 14px;background:#f1f3ff;border:1px solid #dce1ff;border-radius:8px;overflow-wrap:anywhere;word-wrap:break-word;word-break:break-word">
<div style="font-size:12px;color:#5062df;margin-bottom:10px">当前榜首</div>
<div style="font-size:16px;font-weight:700;color:#202743">{escape(str(after['name']))}</div>
<div style="font-size:12px;color:#7580a8;line-height:1.6;margin-top:5px">{escape(organization(after))}</div>
<div style="font-size:26px;font-weight:700;color:#525af4;line-height:1.35;margin-top:14px">{escape(new_score)}</div><div style="font-size:11px;color:#7b83a4">分数</div></td>
</tr></table></td></tr>''')
    plain.extend(["────────────────────", "榜单前十名", "排名 | 团队名称 | 组织 | 分数"])
    table_rows = []
    for row in rows:
        rank, name, org, score = row.get("display_rank", row["rank"]), str(row["name"]), organization(row), shown_score(row)
        plain.append(f"{rank} | {name} | {org} | {score}")
        badge_bg, badge_ink = {1: ("#ebe5ff", "#7251dd"), 2: ("#e5edff", "#426dde"), 3: ("#ddf3fa", "#2589a8")}.get(rank, ("transparent", "#777f92"))
        table_rows.append(f'''<tr style="background:{'#f5f6ff' if rank == 1 else '#ffffff'}">
<td align="center" style="padding:13px 6px;border-bottom:1px solid #eff0f4"><span style="display:inline-block;min-width:24px;line-height:26px;border-radius:6px;font-size:12px;font-weight:700;background:{badge_bg};color:{badge_ink}">{rank}</span></td>
<td class="team-name" style="padding:13px 8px;border-bottom:1px solid #eff0f4;font-size:13px;line-height:1.7;color:#343947;white-space:normal;word-wrap:break-word;overflow-wrap:anywhere;word-break:normal">{escape(name)}</td>
<td class="organization" style="padding:13px 8px;border-bottom:1px solid #eff0f4;font-size:12px;line-height:1.7;color:#747c8e;white-space:normal;word-wrap:break-word;overflow-wrap:anywhere;word-break:normal">{escape(org)}</td>
<td align="right" title="{escape(str(row['score']), quote=True)}" style="padding:13px 10px 13px 6px;border-bottom:1px solid #eff0f4;font-size:14px;line-height:1.7;font-weight:700;color:{'#555df1' if rank == 1 else '#444e66'};font-variant-numeric:tabular-nums;overflow-wrap:anywhere;word-break:break-word">{escape(score)}</td></tr>''')
    if not rows:
        plain.append("暂无可展示的榜单快照。")
        table_rows.append('<tr><td colspan="4" style="padding:24px;color:#80889b">暂无可展示的榜单快照。</td></tr>')
    plain.extend(["", f"榜单：{url}", "", "发信单位：ICTHub", "采样记录反映已公开的成绩变化。"])
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#f5f6fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{escape(title)} · 阿里云天池 · {escape(stamp)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f6fa"><tr><td align="center" style="padding:28px 10px">
<table role="presentation" width="680" cellspacing="0" cellpadding="0" style="width:100%;max-width:680px;background:#ffffff;border:1px solid #e6e9f2;border-radius:12px;overflow:hidden">
<tr><td height="4" bgcolor="#5863ff" style="height:4px;background:#5863ff;background-image:linear-gradient(100deg,#1964ff,#8561ff)"></td></tr>
<tr><td style="padding:26px 24px 28px;background:#f1f4ff;background-image:linear-gradient(125deg,#edf4ff,#f6f2ff,#ffffff)">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td style="font-size:20px;font-weight:800;letter-spacing:.5px;color:#232a3d">ICTHub</td><td align="right" style="font-size:11px;color:#737ea4">阿里云天池 · 赛事成绩通知</td></tr></table>
<div style="margin-top:28px;font-size:11px;line-height:1.7;color:#6875bf">{escape(note)}</div>
<h1 style="margin:9px 0 13px;font-size:30px;line-height:1.3;color:#202634;font-weight:800">{title}</h1>
<div style="font-size:14px;line-height:1.8;color:#48536e;overflow-wrap:anywhere;word-break:break-word">{escape(competition)}</div>
<div style="margin-top:16px;font-size:11px;line-height:1.8;color:#7c85a0">{escape(schedule)} · 快照时间：{escape(stamp)}</div>
</td></tr>
{''.join(changes)}
<tr><td style="padding:0 24px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td height="2" width="25%" bgcolor="#536cff" style="height:2px;background:#536cff"></td><td height="2" bgcolor="#edf0f6" style="height:2px;background:#edf0f6"></td></tr></table></td></tr>
<tr><td style="padding:23px 24px 15px"><h2 style="margin:0;font-size:18px;color:#242b3a">榜单前十名</h2><p style="margin:7px 0 0;font-size:11px;line-height:1.7;color:#8a92a5">{'本次测试实时获取的公开榜单' if test else '与本次榜首变动来自同一次采样'} · {escape(schedule)}</p></td></tr>
<tr><td style="padding:0 16px 24px"><table width="100%" cellspacing="0" cellpadding="0" style="table-layout:fixed;border-collapse:collapse"><thead><tr style="background:#f6f7fa">
<th scope="col" width="12%" style="padding:12px 6px;color:#6f778b;font-size:11px;font-weight:500;white-space:nowrap">排名</th>
<th scope="col" align="left" width="36%" style="padding:12px 8px;color:#6f778b;font-size:11px;font-weight:500;white-space:nowrap">团队名称</th>
<th scope="col" align="left" width="30%" style="padding:12px 8px;color:#6f778b;font-size:11px;font-weight:500;white-space:nowrap">组织</th>
<th scope="col" align="right" width="22%" style="padding:12px 10px 12px 6px;color:#6f778b;font-size:11px;font-weight:500;white-space:nowrap">分数</th>
</tr></thead><tbody>{''.join(table_rows)}</tbody></table></td></tr>
<tr><td align="center" style="padding:0 24px 28px"><a href="{escape(url, quote=True)}" style="display:inline-block;background:#5463ff;background-image:linear-gradient(100deg,#1964ff,#7957ff);color:#ffffff;text-decoration:none;padding:12px 25px;border-radius:6px;font-size:13px;font-weight:600">查看天池实时榜单 →</a></td></tr>
<tr><td style="padding:19px 24px;background:#fafbfe;border-top:1px solid #edf0f5;color:#8d95a7;font-size:11px;line-height:1.9">发信单位：<strong style="color:#5d6884">ICTHub</strong><br>本邮件由 BeauClaw 自动生成，采样记录反映已公开的成绩变化。</td></tr>
</table></td></tr></table></body></html>'''
    return subject, "\n".join(plain) + "\n", html
