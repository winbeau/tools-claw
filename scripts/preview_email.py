"""Generate reviewable email previews using explicitly fictional ranking data."""
from pathlib import Path

from beauclaw.mail import create_message

output = Path(__file__).resolve().parents[1] / 'dist' / 'previews'
output.mkdir(parents=True, exist_ok=True)
rows = [{'rank': i, 'name': name, 'score': score} for i, (name, score) in enumerate([
    ('逐光算力', '98.742631'), ('西域矩阵', '98.521804'), ('向量引擎', '97.886215'),
    ('天山并行', '97.442013'), ('星河计算', '96.901527'), ('极致调优', '95.884621'),
    ('算子先锋', '95.301782'), ('云端编译', '94.775210'), ('北斗计算', '93.684091'),
    ('边界探索', '92.998307')], 1)]
payload = {'preview': True, 'competition_id': '2094722369343447042',
           'competition_name': '2026年CANN挑战赛_西北赛区', 'schedule_name': '初赛',
           'captured_at': '2026-09-07T02:18:32+00:00', 'poll_id': 128,
           'sender': 'preview@example.com', 'recipient': 'reader@example.com', 'top10': rows,
           'events': [{'scope': 'preliminary:realtime_region_ranking',
                       'before': {'name': '西域矩阵', 'score': '98.521804'},
                       'after': rows[0], 'details': {'previous_poll_id': 127}}]}
for kind in ('notification', 'test'):
    message = create_message({**payload, 'test': kind == 'test'}, f'<preview-{kind}@example.com>')
    (output / f'{kind}.html').write_text(message.get_body(preferencelist=('html',)).get_content())
    (output / f'{kind}.eml').write_bytes(message.as_bytes())
print(f'Fictional email previews: {output}')
