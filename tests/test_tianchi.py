import contextlib
import copy
import io
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from beauclaw.auth import save_auth
from beauclaw.cli import main, retry_delay
from beauclaw.core import InvalidBoard, Response, Store, dumps, top_ten
from beauclaw.mail import create_message, load_mail_config, mail_policy
from beauclaw.monitor import Collector
from beauclaw.providers import parse_target
from beauclaw.rankings import Rankings
from beauclaw import tianchi
from test_beauclaw import TIME

URL = 'https://tianchi.aliyun.com/competition/entrance/532499/rankingList'


def teams(count=45, season=1823, event_id=532499):
    return [{'teamId': 1000+i, 'teamName': f'示例队伍{i+1}', 'raceId': event_id,
             'seasonId': season, 'rank': i+1, 'score': str(100-i)+'.1234567891234',
             'teamLeaderOrganization': f'示例组织{i+1}'} for i in range(count)]


def envelope(data):
    return dumps({'success': True, 'code': 'SUCCESS', 'data': data})


def fixture(rows=None, season=1823, event_id='532499', size=20, public=True):
    rows = teams(season=season, event_id=int(event_id)) if rows is None else rows
    detail = {'race': {'raceId': int(event_id), 'name': '示例天池赛事'},
              'showLeaderBoard': public, 'currentSeasonId': season,
              'raceSeasons': [{'hasLeaderBoard': True, 'seasonId': season, 'seasonNum': 0, 'seasonName': '初赛'}]}
    pages = [{'body': envelope({'total': len(rows), 'pageSize': size, 'pageNum': number,
                               'list': rows[(number-1)*size:number*size],
                               'scoreShowConfig': [{'name':'score','leaderboardFormat':'#0.00'}]})}
             for number in range(1, max(1, math.ceil(len(rows)/size))+1)] if public else []
    return {'provider': 'tianchi', 'competition_id': event_id, 'detail': {'body': envelope(detail)},
            'pages': pages, **({'confirmation':copy.deepcopy(pages[0])} if len(pages)>1 else {})}


def captured(bundle=None):
    return Response(TIME, TIME, URL, 200, dumps(fixture() if bundle is None else bundle).encode())


class TianchiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root/'registry.sqlite3'
        self.store = Store(self.db)
        self.store.add_notice('listed@example.com')
        self.config = self.root/'mail.json'
        save_auth(self.config, {'provider':'aliyun','sender':'sender@example.com','password':'synthetic-password'})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_source_url_canonicalization_and_identity_separation(self):
        source, event_id = parse_target(URL+'?spm=tracking#section')
        self.assertEqual((source.id,event_id),('tianchi','532499'))
        self.assertEqual(source.url(event_id),URL)
        for invalid in ('https://tianchi.aliyun.com.evil.test/competition/entrance/532499/rankingList',
                        'http://tianchi.aliyun.com/competition/entrance/532499/rankingList',
                        'https://example.com/532499'):
            with self.assertRaises(ValueError):parse_target(invalid)
        with Rankings(self.db) as registry:
            gitcode = registry.add('532499',name='GitCode fixture')
            aliyun = registry.add(URL,name='Tianchi fixture')
            self.assertNotEqual(gitcode['short_id'],aliyun['short_id'])
            self.assertNotEqual(gitcode['db'],aliyun['db'])
            self.assertEqual(len(registry.list()),2)
            self.assertEqual(registry.add(URL+'?spm=test')['short_id'],aliyun['short_id'])
            registry.delete(gitcode['short_id'])
            self.assertEqual([row['provider'] for row in registry.list()],['tianchi'])

    def test_legacy_registry_migration_preserves_ids_order_and_deleted_rows(self):
        with self.store.db:
            self.store.db.execute('CREATE TABLE rankings(short_id TEXT PRIMARY KEY,competition_id TEXT UNIQUE,name TEXT,url TEXT,storage TEXT,active INTEGER,created_at TEXT,generation INTEGER)')
            self.store.db.execute("INSERT INTO rankings VALUES('abcdef','532499','Existing','https://competition.gitcode.com/competition/532499/live-ranking','rankings/abcdef.sqlite3',1,'2026-01-01',4)")
            self.store.db.execute("INSERT INTO rankings VALUES('123456','42','Deleted','https://competition.gitcode.com/competition/42/live-ranking','rankings/123456.sqlite3',0,'2026-01-02',5)")
            self.store.db.execute("INSERT INTO meta VALUES('ranking_registry_initialized','1')")
        before = [tuple(row) for row in self.store.db.execute('SELECT * FROM rankings ORDER BY short_id')]
        with Rankings(self.db) as registry:
            self.assertEqual([tuple(row)[:-1] for row in registry.db.execute('SELECT * FROM rankings ORDER BY short_id')],before)
            self.assertEqual([row['provider'] for row in registry.list(include_deleted=True)],['gitcode','gitcode'])
            registry.add(URL,name='Tianchi fixture with the same numeric ID')
            self.assertEqual(len(registry.list()),2)
            self.assertEqual(registry.get('abcdef')['generation'],4)

    def test_cli_add_reads_the_tianchi_title_then_lists_and_deletes_by_hash(self):
        output=io.StringIO()
        metadata=Response(TIME,TIME,tianchi.API+'getDetail?raceId=532499',200,fixture()['detail']['body'].encode())
        argv=['beauclaw','ranking','add',URL,'--db',str(self.db)]
        with patch('sys.argv',argv), contextlib.redirect_stdout(output), \
             patch('beauclaw.tianchi.fetch_url',return_value=metadata) as fetch:
            self.assertEqual(main(),0)
        self.assertEqual(fetch.call_args.args[0],tianchi.API+'getDetail?raceId=532499')
        with Rankings(self.db) as registry:
            row=registry.list()[0]
        expected=f"{row['short_id']}-示例天池赛事 -> {URL}"
        self.assertIn(expected,output.getvalue())
        self.assertEqual(row['provider'],'tianchi')
        output=io.StringIO()
        with patch('sys.argv',['beauclaw','ranking','list','--db',str(self.db)]), contextlib.redirect_stdout(output):
            self.assertEqual(main(),0)
        self.assertEqual(output.getvalue().strip(),expected)
        with patch('sys.argv',['beauclaw','ranking','delete',row['short_id'],'--db',str(self.db)]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(),0)
        with Rankings(self.db) as registry:
            self.assertEqual(registry.list(),[])

    def test_complete_pagination_ties_organization_and_exact_scores(self):
        rows = teams()
        rows[2]['rank'] = 2
        rows[2]['score'] = rows[1]['score']
        rows[3].pop('teamLeaderOrganization')
        board = tianchi.parse_board(captured(fixture(rows)).body,TIME)
        self.assertEqual(board['total'],45)
        top = top_ten(board)
        self.assertEqual(len(top),10)
        self.assertEqual(top[0]['score'],'100.1234567891234')
        self.assertEqual(top[0]['display_score'],'100.12')
        self.assertEqual(top[2]['rank'],3)
        self.assertEqual(top[2]['display_rank'],2)
        self.assertEqual(top[3]['organization'],'')
        self.assertEqual(top[0]['organization'],'示例组织1')

    def test_malformed_or_partial_pages_do_not_advance_the_baseline(self):
        path=self.root/'tianchi.sqlite3'
        with contextlib.closing(Store(path,'532499',provider='tianchi')) as history:
            history.record(captured())
            original=history.summary()['states']
            broken=[]
            missing=fixture();missing['pages'].pop();broken.append(missing)
            duplicate=fixture();page=json.loads(duplicate['pages'][1]['body']);page['data']['list'][0]['teamId']=1000;duplicate['pages'][1]['body']=dumps(page);broken.append(duplicate)
            foreign=fixture();page=json.loads(foreign['pages'][1]['body']);page['data']['list'][0]['raceId']=999;foreign['pages'][1]['body']=dumps(page);broken.append(foreign)
            incomplete=fixture();page=json.loads(incomplete['pages'][1]['body']);page['data']['total']+=1;incomplete['pages'][1]['body']=dumps(page);broken.append(incomplete)
            changed=fixture();page=json.loads(changed['confirmation']['body']);page['data']['list'][0]['score']='200';changed['confirmation']['body']=dumps(page);broken.append(changed)
            bad_score=fixture(teams(1));page=json.loads(bad_score['pages'][0]['body']);page['data']['list'][0]['score']='NaN';bad_score['pages'][0]['body']=dumps(page);broken.append(bad_score)
            for field in ('teamId','score'):
                bad_rows=teams(1);bad_rows[0].pop(field);broken.append(fixture(bad_rows))
            bad_name=teams(1);bad_name[0]['teamName']={'unexpected':'object'};broken.append(fixture(bad_name))
            for value in broken:
                poll,events=history.record(captured(value))
                self.assertEqual(poll['status'],'error')
                self.assertEqual(events,[])
                self.assertEqual(history.summary()['states'],original)
            for value in (fixture([]),fixture(public=False)):
                poll,events=history.record(captured(value))
                self.assertEqual(poll['status'],'unavailable')
                self.assertEqual(events,[])
                self.assertEqual(history.summary()['states'],original)

    def test_missing_profiles_preserve_identity_and_history_across_restart(self):
        with Rankings(self.db) as registry:
            row=registry.add(URL,name='Tianchi fixture')
        path=Path(row['db'])
        policy=mail_policy(load_mail_config(self.config),provider='tianchi')
        rows=teams(12)
        with contextlib.closing(Store(path,'532499',notice_db=self.db,provider='tianchi')) as history:
            history.record(captured(fixture(rows)),mail_policy=policy)
            for position in (0,7):
                rows[position].pop('teamName')
                rows[position].pop('teamLeaderOrganization')
            sample=captured(fixture(rows))
            poll,events=history.record(sample,mail_policy=policy)
            self.assertEqual(poll['status'],'ok')
            self.assertEqual(poll['info']['counts']['leaderboard'],12)
            self.assertEqual(events,[])
            self.assertFalse(poll['critical'])
            self.assertEqual(history.summary()['mail_pending'],0)
            self.assertEqual(history.snapshot(poll['id'])['raw_body'],sample.body.decode())
            entry=history.summary()['states'][0]['members']['team_id:1007']['entry']
            self.assertEqual((entry['name'],entry['name_source']),('示例队伍8','history'))
        with contextlib.closing(Store(path,'532499',notice_db=self.db,provider='tianchi')) as history:
            poll,events=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertEqual(events,[])
            rows[0]['score']='99.999'
            dropped,_=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertTrue(dropped['critical'])
            self.assertEqual(history.summary()['mail_pending'],0)
            rows[0]['score']='100.001'
            raised,_=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertTrue(raised['critical'])
            self.assertEqual(history.summary()['mail_pending'],1)
            payload=json.loads(history.db.execute('SELECT payload_json FROM mail_outbox').fetchone()[0])
            self.assertEqual(payload['top10'][0]['name_source'],'history')
            message=create_message(payload,'<missing-profile@example.com>')
            for part in ('html','plain'):
                self.assertIn('示例队伍1（上次公开队名）',message.get_body(preferencelist=(part,)).get_content())
            rows[0]['teamName']='示例队伍1'
            _,events=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertNotIn('leader_changed',[event['kind'] for event in events])
            self.assertEqual(history.summary()['mail_pending'],1)
            rows[0].pop('teamName')
            history.record(captured(fixture(rows)),mail_policy=policy)
            rows[0]['teamName']='实际改名后的队伍'
            renamed,events=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertTrue(renamed['critical'])
            self.assertIn('renamed',[event['kind'] for event in events])
            self.assertEqual(history.summary()['mail_pending'],2)

    def test_first_public_name_is_not_a_rename_but_anonymous_leader_replacement_notifies(self):
        with Rankings(self.db) as registry:
            row=registry.add(URL,name='Tianchi fixture')
        policy=mail_policy(load_mail_config(self.config),provider='tianchi')
        with contextlib.closing(Store(Path(row['db']),'532499',notice_db=self.db,provider='tianchi')) as history:
            rows=teams(12);rows[0]['teamName']=None
            poll,_=history.record(captured(fixture(rows)),mail_policy=policy)
            entry=history.summary()['states'][0]['members']['team_id:1000']['entry']
            self.assertEqual(entry['name_source'],'unavailable')
            self.assertIn('ID 1000',entry['name'])
            rows[0]['teamName']='首次公开的队名'
            poll,events=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertEqual(events,[])
            self.assertFalse(poll['critical'])
            self.assertEqual(history.summary()['mail_pending'],0)
            rows[0].update(teamId=2000,teamName=' ',score='99.999')
            poll,_=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertTrue(poll['critical'])
            self.assertEqual(history.summary()['mail_pending'],1)
            rows[0]['teamName']='新榜首首次公开队名'
            poll,events=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertNotIn('leader_changed',[event['kind'] for event in events])
            self.assertFalse(poll['critical'])
            self.assertEqual(history.summary()['mail_pending'],1)

    def test_fetch_all_pages_without_credentials_and_preserve_rate_limit(self):
        value=fixture()
        requests=[]
        def respond(url,headers,timeout):
            requests.append((url,headers,timeout))
            params=parse_qs(urlsplit(url).query)
            body=value['detail']['body'] if 'getDetail' in url else value['pages'][int(params['pageNum'][0])-1]['body']
            return Response(TIME,TIME,url,200,body.encode())
        with patch('beauclaw.tianchi.fetch_url',side_effect=respond):
            sample=tianchi.fetch('532499',{'token':'private-gitcode-token','cookie':'private-gitcode-cookie'})
        self.assertIsNone(sample.error)
        self.assertEqual(len(requests),5)  # Detail, 3 pages and page-1 confirmation.
        self.assertEqual(tianchi.parse_board(sample.body,TIME)['total'],45)
        self.assertNotIn('private-gitcode',sample.body.decode())
        for _,headers,timeout in requests:
            self.assertNotIn('Authorization',headers)
            self.assertNotIn('Cookie',headers)
            self.assertGreater(timeout,0)
        def limited(url,headers,timeout):
            if 'pageNum=2' in url:
                return Response(TIME,TIME,url,429,b'{}',{'retry-after':'120'})
            return respond(url,headers,timeout)
        with patch('beauclaw.tianchi.fetch_url',side_effect=limited):
            sample=tianchi.fetch('532499')
        self.assertIsNotNone(sample.error)
        self.assertEqual(sample.status,429)
        self.assertEqual(retry_delay(sample,1,10),120)
        with patch('beauclaw.tianchi.fetch_url',side_effect=respond), \
             patch('beauclaw.tianchi.time.monotonic',side_effect=[0,0,9]):
            expired=tianchi.fetch('532499',timeout=8)
        self.assertIn('exceeded the request timeout',expired.error)

    def test_provider_binding_prevents_history_mixing(self):
        path=self.root/'tianchi.sqlite3'
        with contextlib.closing(Store(path,'532499',provider='tianchi')) as history:
            poll,_=history.record(captured(fixture(event_id='532500')))
            self.assertEqual(poll['status'],'error')
            self.assertEqual(history.summary()['states'],[])
        with self.assertRaisesRegex(ValueError,'another competition or provider'):
            Store(path,'532499',provider='gitcode')
        with contextlib.closing(Store(path)) as history:
            self.assertEqual(history.provider,'tianchi')

    def test_tianchi_notifications_retention_and_season_baseline(self):
        with Rankings(self.db) as registry:
            row=registry.add(URL,name='Tianchi fixture')
        policy=mail_policy(load_mail_config(self.config),provider='tianchi')
        with contextlib.closing(Store(Path(row['db']),'532499',notice_db=self.db,provider='tianchi')) as history:
            rows=teams(12)
            rows[0].update(teamName='<script>示例榜首</script>',teamLeaderOrganization='组织 <b>示例</b>',score='100.001')
            history.record(captured(fixture(rows)),mail_policy=policy)
            rows[0]['score']='99.999'
            dropped,_=history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertTrue(dropped['critical'])
            self.assertEqual(history.summary()['mail_pending'],0)
            rows[0]['score']='100.001'
            raised,_=history.record(captured(fixture(rows)),mail_policy=policy)
            rows[0]['score']='100.002'
            history.record(captured(fixture(rows)),mail_policy=policy)
            for _ in range(110):history.record(captured(fixture(rows)),mail_policy=policy)
            self.assertEqual(history.summary()['poll_count']-history.summary()['critical_poll_count'],100)
            self.assertTrue(history.snapshot(dropped['id'])['critical'])
            self.assertTrue(history.snapshot(raised['id'])['critical'])
            self.assertEqual(history.summary()['mail_pending'],2)
            queued=history.db.execute('SELECT * FROM mail_outbox ORDER BY id DESC LIMIT 1').fetchone()
            data=json.loads(queued['payload_json'])
            self.assertEqual(data['provider'],'tianchi')
            self.assertEqual(len(data['top10']),10)
            message=create_message(data,queued['message_id'])
            html=message.get_body(preferencelist=('html',)).get_content()
            self.assertEqual(html.count('<th scope="col"'),4)
            for heading in ('排名','团队名称','组织','分数'):self.assertIn('>'+heading+'</th>',html)
            self.assertIn('&lt;script&gt;',html)
            self.assertIn('组织 &lt;b&gt;示例&lt;/b&gt;',html)
            self.assertNotIn('<script>',html)
            self.assertIn('100.001',html)
            self.assertIn('100.002',html)
            self.assertIn(URL,html)
            self.assertNotIn('competition.gitcode.com',html)
            self.assertEqual(message['Importance'],'high')
            history.record(captured(fixture(season=2000)),mail_policy=policy)
            self.assertEqual(history.summary()['mail_pending'],2)

    def test_targeted_test_can_select_tianchi_without_gitcode_login(self):
        with Rankings(self.db) as registry:
            registry.add('532499',name='First GitCode competition')
            row=registry.add(URL,name='Second Tianchi competition')
        notices=self.store.notices()
        argv=['beauclaw','notice','test','only@example.com','--ranking',row['short_id'],
              '--db',str(self.db),'--mail-config',str(self.config),'--token-file',str(self.root/'missing-token')]
        with patch('sys.argv',argv), contextlib.redirect_stdout(io.StringIO()), \
             patch('beauclaw.mail.load_auth',side_effect=AssertionError('Tianchi must not read GitCode credentials')), \
             patch('beauclaw.tianchi.fetch',return_value=captured()) as fetch, patch('beauclaw.mail.send_message') as send:
            self.assertEqual(main(),0)
        fetch.assert_called_once()
        self.assertEqual(send.call_count,1)
        self.assertEqual(send.call_args.args[3],'only@example.com')
        html=send.call_args.args[1].get_body(preferencelist=('html',)).get_content()
        self.assertIn('阿里云天池',html)
        self.assertEqual(html.count('<th scope="col"'),4)
        self.assertNotIn('当前榜首',html)
        self.assertIsNone(send.call_args.args[1]['Importance'])
        self.assertEqual(self.store.notices(),notices)

    def test_bad_gitcode_auth_does_not_block_public_tianchi_collector(self):
        with Rankings(self.db) as registry:
            gitcode=registry.add('532499',name='GitCode fixture')
            aliyun=registry.add(URL,name='Tianchi fixture')
        token=self.root/'empty-token';token.touch()
        args=SimpleNamespace(db=self.db,interval=10,timeout=8,missing_samples=2,no_mail=True,
                             mail_config=self.config,auth_file=self.root/'missing-auth',token_file=token,once=True,samples=0)
        workers=[Collector(row,args) for row in (gitcode,aliyun)]
        with patch('beauclaw.tianchi.fetch',return_value=captured()):
            for worker in workers:worker.start()
            for worker in workers:worker.join(5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        with contextlib.closing(Store(Path(gitcode['db']))) as history:
            self.assertEqual(history.summary()['latest']['status'],'error')
        with contextlib.closing(Store(Path(aliyun['db']))) as history:
            self.assertEqual(history.summary()['latest']['status'],'ok')
            self.assertEqual(history.summary()['provider'],'tianchi')


if __name__=='__main__':unittest.main()
