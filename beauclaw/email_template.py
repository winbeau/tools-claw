"""Chinese ICTHub emails with inline styles and a plain-text alternative."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import re
from urllib.parse import quote

from beauclaw.core import DEFAULT_COMPETITION, utcnow


def render_email(payload: dict) -> tuple[str, str, str]:
    test = bool(payload.get("test"))
    preview = bool(payload.get("preview"))
    title = "前十名榜单" if test else "榜一变动"
    schedule = str(payload.get("schedule_name") or "CANN 挑战赛").replace("\r", " ").replace("\n", " ")[:80]
    competition = str(payload.get("competition_name") or "CANN 挑战赛").replace("\r", " ").replace("\n", " ")[:120]
    match = re.search(r"(西北|东北|华北|华东|华中|华南|西南)赛区", competition)
    region = match.group() if match else "赛区榜单"
    observed = datetime.fromisoformat(payload.get("captured_at") or utcnow()).astimezone(timezone(timedelta(hours=8)))
    stamp = f"{observed:%Y-%m-%d %H:%M:%S}（北京时间）"
    url = "https://competition.gitcode.com/competition/" + quote(str(payload.get("competition_id") or DEFAULT_COMPETITION), safe="") + "/live-ranking"
    rows = sorted(payload.get("top10") or [], key=lambda row: row["rank"])[:10]
    subject = f"[ICTHub] 榜单测试 · {competition}" if test else f"[ICTHub · 关键邮件 · 榜一变动] {competition} · {schedule} · 快照 #{payload['poll_id']}"
    lines = ["ICTHub", "样式预览 · 以下均为示例数据" if preview else title, f"{region} · 参赛区域实时总榜",
             f"赛程：{schedule}", f"快照时间：{stamp}", ""]
    sections = []
    if not test:
        lines.extend(["关键邮件 · 榜首变化记录及变动前后快照永久保留", ""])
        for event in payload["events"]:
            before, after = event["before"], event["after"]
            lines.extend([f"变化前：{before['name']}，分数 {before['score']}",
                          f"变化后：{after['name']}，分数 {after['score']}",
                          f"变化前快照：{event['details']['previous_poll_id']}", ""])
            sections.append(f'''<tr><td style="padding:28px 32px 24px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="table-layout:fixed"><tr>
<td valign="top" width="44%" style="padding:18px 14px;background:#f5f6f9;border:1px solid #e9ebf0;border-radius:12px;overflow-wrap:anywhere;word-break:break-word">
<div style="color:#707889;font-size:12px;margin-bottom:10px">变化前</div><div style="font-size:16px;font-weight:700;color:#252b3a">{escape(before['name'])}</div>
<div style="margin-top:12px;font-size:25px;font-weight:700;color:#657083;font-variant-numeric:tabular-nums">{escape(before['score'])}</div><div style="font-size:11px;color:#87909e;margin-top:3px">分数</div></td>
<td width="12%" align="center" style="color:#d74730;font-size:24px">→</td>
<td valign="top" width="44%" style="padding:18px 14px;background:#fff4ed;border:1px solid #f7d2bd;border-radius:12px;overflow-wrap:anywhere;word-break:break-word">
<div style="color:#c34525;font-size:12px;margin-bottom:10px">当前榜首</div><div style="font-size:16px;font-weight:700;color:#252b3a">{escape(after['name'])}</div>
<div style="margin-top:12px;font-size:25px;font-weight:700;color:#ce422a;font-variant-numeric:tabular-nums">{escape(after['score'])}</div><div style="font-size:11px;color:#a76754;margin-top:3px">分数</div></td>
</tr></table></td></tr>''')
    lines.extend(["────────────────────", f"{region} · 前十名", "序号 | 队名 | 分数"])
    table_rows = []
    for row in rows:
        rank, name, score = row["rank"], str(row["name"]), str(row["score"])
        lines.append(f"{rank} | {name} | {score}")
        background = "#fff6ef" if rank == 1 else "#ffffff" if rank % 2 else "#fafbfc"
        badge = {1: ("#f5ab39", "#582d00"), 2: ("#e7ebf1", "#4d5b70"), 3: ("#f0ddca", "#7b5130")}.get(rank, ("transparent", "#768092"))
        table_rows.append(f'''<tr style="background:{background}"><td align="center" style="padding:13px 8px;border-bottom:1px solid #eef0f4"><span style="display:inline-block;width:28px;line-height:28px;text-align:center;border-radius:8px;background:{badge[0]};color:{badge[1]};font-size:13px;font-weight:700">{rank}</span></td><td class="team-name" style="padding:13px 8px;border-bottom:1px solid #eef0f4;color:#252b3a;font-size:14px;white-space:normal;word-wrap:break-word;overflow-wrap:anywhere;word-break:normal">{escape(name)}</td><td align="right" style="padding:13px 12px 13px 8px;border-bottom:1px solid #eef0f4;font-size:14px;font-weight:700;color:{'#ce422a' if rank == 1 else '#364258'};font-variant-numeric:tabular-nums;overflow-wrap:anywhere;word-break:break-word">{escape(score)}</td></tr>''')
    if not rows:
        empty = f"暂无可展示的{region}快照。启动采集后，通知将在这里展示对应快照的前十名。" if test else "此历史通知的快照未包含可展示的前十名。"
        lines.append(empty)
        table_rows.append(f'<tr><td colspan="3" style="padding:24px 16px;color:#707889;font-size:13px;line-height:1.8">{empty}</td></tr>')
    note = "样式预览 · 以下均为示例数据" if preview else "测试邮件 · 当前榜单" if test else "关键邮件 · 榜首变化快照永久保留"
    lines.extend(["", f"榜单：{url}", "", "发信单位：ICTHub", "采样记录反映已公开的成绩变化。"])
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#f4f6fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{escape(title)} · {region} · {escape(stamp)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6fa"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="640" cellspacing="0" cellpadding="0" style="width:100%;max-width:640px;background:#ffffff;border:1px solid #e4e8f0;border-radius:18px;overflow:hidden">
<tr><td bgcolor="#d93b31" style="padding:28px 32px;background:#d93b31;background-image:linear-gradient(115deg,#ef7d17,#d92140);border-radius:17px 17px 0 0;color:#ffffff">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td style="font-size:20px;font-weight:800;letter-spacing:0.5px;color:#ffffff">ICTHub</td><td align="right" style="font-size:12px;color:#ffffff">赛事成绩通知</td></tr></table>
<div style="margin-top:24px;font-size:12px;color:#ffffff">{region} · {escape(competition)} · {escape(schedule)}</div>
<h1 style="margin:10px 0 12px;font-size:32px;line-height:1.3;font-weight:800;color:#ffffff">{title}</h1>
<span style="display:inline-block;padding:5px 10px;border:1px solid #fba088;border-radius:20px;background:#c83c32;font-size:12px;line-height:1.5;color:#ffffff">{note}</span>
<div style="margin-top:18px;font-size:12px;line-height:1.7;color:#ffffff">快照时间：{escape(stamp)}</div>
</td></tr>
{''.join(sections)}
<tr><td style="padding:0 32px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td height="2" bgcolor="#ef7d17" width="24%" style="height:2px;background:#ef7d17"></td><td height="2" bgcolor="#e9edf4" style="height:2px;background:#e9edf4"></td></tr></table></td></tr>
<tr><td style="padding:24px 32px 12px"><h2 style="margin:0;color:#252b3a;font-size:19px;font-weight:750">{region} · 前十名</h2><p style="margin:7px 0 0;color:#818a99;font-size:12px;line-height:1.6">{'本次测试实时获取的榜单' if test else '与本次榜首变动来自同一次采样'} · 序号按赛事榜单顺序</p></td></tr>
<tr><td style="padding:0 24px 20px"><table width="100%" cellspacing="0" cellpadding="0" style="table-layout:fixed;border-collapse:collapse"><thead><tr style="background:#f1f3f8"><th scope="col" width="12%" style="white-space:nowrap;padding:12px 8px;font-size:12px;font-weight:500;color:#737e91">序号</th><th scope="col" align="left" width="56%" style="white-space:nowrap;padding:12px 8px;font-size:12px;font-weight:500;color:#737e91">队名</th><th scope="col" align="right" width="32%" style="white-space:nowrap;padding:12px 12px 12px 8px;font-size:12px;font-weight:500;color:#737e91">分数</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></td></tr>
<tr><td align="center" style="padding:6px 24px 28px"><a href="{escape(url, quote=True)}" style="display:inline-block;background:#6746e8;color:#ffffff;text-decoration:none;border-radius:10px;padding:13px 28px;font-size:14px;font-weight:600">查看实时榜单 →</a></td></tr>
<tr><td style="padding:20px 32px;background:#fafbfe;border-top:1px solid #edf0f5;color:#8a93a3;font-size:11px;line-height:1.9">发信单位：<strong style="color:#536076">ICTHub</strong><br>本邮件由 BeauClaw 自动生成，采样记录反映已公开的成绩变化。</td></tr>
</table></td></tr></table></body></html>'''
    return subject, "\n".join(lines) + "\n", html
