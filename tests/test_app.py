import hashlib, hmac, json, time, urllib.parse, os, asyncio, io, re
from pathlib import Path
import pytest
os.environ['TELEGRAM_BOT_TOKEN']='test-token'
os.environ['ADMIN_TELEGRAM_IDS']='9001'
os.environ['FRONTEND_DIR']=str(Path(__file__).resolve().parents[1]/'frontend')
os.environ['MEDIA_DIR']=str(Path(__file__).resolve().parents[1]/'media_test')
os.environ.pop('SUPABASE_URL',None)
os.environ.pop('SUPABASE_SECRET_KEY',None)
os.environ['DATABASE_URL']=f"sqlite:///{Path(__file__).resolve().parents[1]/'data_test'/'taxi.db'}"
from fastapi.testclient import TestClient
from PIL import Image
import backend.app.main as appmod

TOKEN='test-token'

def init_data(uid, age=0):
    q={'auth_date':str(int(time.time())-age),'query_id':f'Q{uid}',
       'user':json.dumps({'id':uid,'first_name':f'U{uid}','last_name':'Test','username':f'u{uid}'},separators=(',',':'))}
    canonical='\n'.join(f'{k}={q[k]}' for k in sorted(q))
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    q['hash']=hmac.new(secret,canonical.encode(),hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(q)

def headers(uid): return {'X-Telegram-Init-Data':init_data(uid)}

@pytest.fixture(autouse=True)
def clean_db():
    asyncio.run(appmod.init_db())
    with appmod.engine.begin() as c:
        for table in ('admin_actions','commission_payments','ratings','messages','trips','offers','payments','driver_locations','orders','users'):
            c.exec_driver_sql(f'DELETE FROM {table}')

def client(): return TestClient(appmod.app)

def make_driver(c, uid=1002):
    h=headers(uid)
    assert c.post('/api/me/role',headers=h,json={'role':'driver'}).status_code==200
    did=c.get('/api/me',headers=h).json()['id']
    assert c.post(f'/api/admin/drivers/{did}/decision',headers=headers(9001),json={'approved':True}).status_code==200
    return h,did

def make_passenger(c, uid=1001):
    h=headers(uid); assert c.post('/api/me/role',headers=h,json={'role':'passenger'}).status_code==200; return h

def test_01_health_root_api():
    with client() as c:
        assert c.get('/api/health').json()['version']=='5.0.0'
        assert c.get('/').status_code==200

def test_02_auth_boundaries():
    with client() as c:
        assert c.get('/api/me').status_code==401
        bad=urllib.parse.parse_qs(init_data(1)); bad['hash']=['bad']
        assert c.get('/api/me',headers={'X-Telegram-Init-Data':urllib.parse.urlencode(bad,doseq=True)}).status_code==401
        assert c.get('/api/me',headers=headers(1)).status_code==200

def test_03_expired_and_future_auth():
    with client() as c:
        assert c.get('/api/me',headers=headers(2)).status_code==200
        assert c.get('/api/me',headers={'X-Telegram-Init-Data':init_data(2,age=3700)}).status_code==401

def test_04_profile_all_fields_and_upload_security():
    with client() as c:
        h=make_passenger(c,1010)
        r=c.post('/api/me/profile',headers=h,json={'name':'  Ivan  ','phone':'+79990000000','about':'hello','car_make':'Lada','car_model':'Vesta','car_plate':'A123BC'}); assert r.status_code==200
        m=c.get('/api/me',headers=h).json()
        assert m['name']=='Ivan' and m['phone']=='+79990000000' and m['car_make']=='Lada' and m['car_plate']=='A123BC'
        bad=c.post('/api/me/photo',headers=h,files={'file':('x.txt',b'hello','text/plain')}); assert bad.status_code==415
        bio=io.BytesIO(); Image.new('RGB',(2,2),'white').save(bio,format='PNG'); bio.seek(0)
        ok=c.post('/api/me/photo',headers=h,files={'file':('x.png',bio.getvalue(),'image/png')}); assert ok.status_code==200 and ok.json()['photo_url'].startswith('/media/')

def test_05_driver_approval_and_online_gate():
    with client() as c:
        h,did=make_driver(c,1020)
        assert c.post('/api/driver/online',headers=h).status_code==200
        assert c.get('/api/orders/active',headers=h).status_code==200

def test_06_order_all_fields_and_duplicate_active_block():
    with client() as c:
        p=make_passenger(c,1030)
        payload={'pickup_lat':42.99,'pickup_lng':44.15,'pickup_address':'A street','destination_lat':43.0,'destination_lng':44.16,'destination_address':'B street'}
        r=c.post('/api/orders',headers=p,json=payload); assert r.status_code==200
        assert c.post('/api/orders',headers=p,json=payload).status_code==409
        oid=r.json()['order_id']; row=c.get('/api/orders/'+str(oid),headers=p); assert row.status_code==200
        data=row.json(); assert data['pickup_lat']==42.99 and data['destination_lng']==44.16

def test_07_offer_fields_duplicate_and_accept():
    with client() as c:
        p=make_passenger(c,1040); d,did=make_driver(c,1041); c.post('/api/driver/online',headers=d)
        oid=c.post('/api/orders',headers=p,json={'pickup_lat':1,'pickup_lng':2,'pickup_address':'A','destination_lat':3,'destination_lng':4,'destination_address':'B'}).json()['order_id']
        r=c.post('/api/offers',headers=d,json={'order_id':oid,'price':'150.25','eta_min':7}); assert r.status_code==200
        assert c.post('/api/offers',headers=d,json={'order_id':oid,'price':'151','eta_min':8}).status_code==409
        off=r.json()['offer_id']; assert c.post(f'/api/offers/{off}/accept',headers=p).status_code==200
        assert c.get(f'/api/orders/{oid}',headers=p).json()['price']==150.25

def test_08_concurrent_offer_acceptance():
    with client() as c:
        p=make_passenger(c,1050); d,did=make_driver(c,1051); c.post('/api/driver/online',headers=d)
        oid=c.post('/api/orders',headers=p,json={'pickup_lat':1,'pickup_lng':2,'pickup_address':'A','destination_lat':3,'destination_lng':4,'destination_address':'B'}).json()['order_id']
        off=c.post('/api/offers',headers=d,json={'order_id':oid,'price':150,'eta_min':5}).json()['offer_id']
        import concurrent.futures
        def one():
            with client() as cc: return cc.post(f'/api/offers/{off}/accept',headers=p).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex: codes=sorted(ex.map(lambda _:one(),range(2)))
        assert codes==[200,409]

def test_09_status_machine_and_commission():
    with client() as c:
        p=make_passenger(c,1060); d,did=make_driver(c,1061); c.post('/api/driver/online',headers=d)
        oid=c.post('/api/orders',headers=p,json={'pickup_lat':1,'pickup_lng':2,'pickup_address':'A','destination_lat':3,'destination_lng':4,'destination_address':'B'}).json()['order_id']
        off=c.post('/api/offers',headers=d,json={'order_id':oid,'price':200,'eta_min':5}).json()['offer_id']; c.post(f'/api/offers/{off}/accept',headers=p)
        assert c.post(f'/api/orders/{oid}/status',headers=d,json={'status':'COMPLETED'}).status_code==409
        assert c.post(f'/api/orders/{oid}/status',headers=d,json={'status':'DRIVER_ARRIVED'}).status_code==200
        assert c.post(f'/api/orders/{oid}/status',headers=d,json={'status':'TRIP_STARTED'}).status_code==200
        assert c.post(f'/api/orders/{oid}/status',headers=d,json={'status':'COMPLETED'}).status_code==200
        assert c.get('/api/driver/finance',headers=d).json()['commission_balance']==20.0

def test_10_rating_once_and_average():
    with client() as c:
        p=make_passenger(c,1070); d,did=make_driver(c,1071); c.post('/api/driver/online',headers=d)
        oid=c.post('/api/orders',headers=p,json={'pickup_lat':1,'pickup_lng':2,'pickup_address':'A','destination_lat':3,'destination_lng':4,'destination_address':'B'}).json()['order_id']; off=c.post('/api/offers',headers=d,json={'order_id':oid,'price':100,'eta_min':5}).json()['offer_id']; c.post(f'/api/offers/{off}/accept',headers=p)
        for st in ('DRIVER_ARRIVED','TRIP_STARTED','COMPLETED'): c.post(f'/api/orders/{oid}/status',headers=d,json={'status':st})
        assert c.post(f'/api/orders/{oid}/rating',headers=p,json={'stars':4,'comment':'ok'}).status_code==200
        assert c.post(f'/api/orders/{oid}/rating',headers=p,json={'stars':5}).status_code==409
        assert c.get('/api/me',headers=d).json()['rating']==4.0

def test_11_location_all_fields_and_acl():
    with client() as c:
        p=make_passenger(c,1080); d,did=make_driver(c,1081); c.post('/api/driver/online',headers=d)
        assert c.post('/api/driver/location',headers=d,json={'lat':42.99,'lng':44.15,'speed':12.3,'course':180,'accuracy':5}).status_code==200
        assert c.get(f'/api/drivers/{did}/location',headers=p).status_code==403
        assert c.get(f'/api/drivers/{did}/location',headers=d).status_code==200

def test_12_input_validation_coordinates_location_messages():
    with client() as c:
        d,did=make_driver(c,1090)
        assert c.post('/api/driver/location',headers=d,json={'lat':91,'lng':0}).status_code==422
        assert c.post('/api/driver/location',headers=d,json={'lat':0,'lng':0,'accuracy':-1}).status_code==422
        p=make_passenger(c,1091); assert c.post('/api/orders',headers=p,json={'pickup_lat':0,'pickup_lng':0,'pickup_address':'','destination_lat':0,'destination_lng':0,'destination_address':'B'}).status_code==422

def test_13_chat_participant_boundary():
    with client() as c:
        p=make_passenger(c,1100); p2=make_passenger(c,1101); d,did=make_driver(c,1102); c.post('/api/driver/online',headers=d)
        oid=c.post('/api/orders',headers=p,json={'pickup_lat':1,'pickup_lng':2,'pickup_address':'A','destination_lat':3,'destination_lng':4,'destination_address':'B'}).json()['order_id']; off=c.post('/api/offers',headers=d,json={'order_id':oid,'price':120,'eta_min':5}).json()['offer_id']; c.post(f'/api/offers/{off}/accept',headers=p)
        assert c.post(f'/api/orders/{oid}/messages',headers=p,json={'body':'hello'}).status_code==200
        assert c.get(f'/api/orders/{oid}/messages',headers=p2).status_code==403

def test_14_admin_only():
    with client() as c:
        p=make_passenger(c,1110)
        assert c.get('/api/admin/drivers',headers=p).status_code==403
        assert c.get('/api/admin/summary',headers=headers(9001)).status_code==200

def test_15_admin_block_stops_driver():
    with client() as c:
        d,did=make_driver(c,1120); c.post('/api/driver/online',headers=d)
        r=c.post(f'/api/admin/drivers/{did}/block',headers=headers(9001),json={'blocked':True}); assert r.status_code==200
        assert r.json()['blocked'] is True
        assert c.post('/api/driver/online',headers=d).status_code==403
        assert c.post('/api/driver/location',headers=d,json={'lat':1,'lng':2}).status_code==403

def test_16_admin_unblock_clears_debt_and_waives_pending():
    with client() as c:
        p=make_passenger(c,1130); d,did=make_driver(c,1131); c.post('/api/driver/online',headers=d)
        oid=c.post('/api/orders',headers=p,json={'pickup_lat':1,'pickup_lng':2,'pickup_address':'A','destination_lat':3,'destination_lng':4,'destination_address':'B'}).json()['order_id']; off=c.post('/api/offers',headers=d,json={'order_id':oid,'price':300,'eta_min':5}).json()['offer_id']; c.post(f'/api/offers/{off}/accept',headers=p)
        for st in ('DRIVER_ARRIVED','TRIP_STARTED','COMPLETED'): c.post(f'/api/orders/{oid}/status',headers=d,json={'status':st})
        assert c.get('/api/driver/finance',headers=d).json()['commission_balance']==30.0
        c.post(f'/api/admin/drivers/{did}/block',headers=headers(9001),json={'blocked':True})
        r=c.post(f'/api/admin/drivers/{did}/block',headers=headers(9001),json={'blocked':False}); assert r.status_code==200 and r.json()['commission_balance']==0
        fin=c.get('/api/driver/finance',headers=d).json(); assert fin['commission_balance']==0 and fin['blocked'] is False
        with appmod.engine.begin() as db:
            status=db.exec_driver_sql('SELECT status FROM commission_payments WHERE driver_id=? ORDER BY id DESC LIMIT 1',(did,)).scalar()
            action=db.exec_driver_sql('SELECT action,debt_before,debt_after FROM admin_actions WHERE target_driver_id=? ORDER BY id DESC LIMIT 1',(did,)).fetchone()
        assert status in (None,'WAIVED') and action[0]=='UNBLOCK' and float(action[1])==30 and float(action[2])==0

def test_17_admin_reblock_after_clear():
    with client() as c:
        d,did=make_driver(c,1140)
        c.post(f'/api/admin/drivers/{did}/block',headers=headers(9001),json={'blocked':True})
        r=c.post(f'/api/admin/drivers/{did}/block',headers=headers(9001),json={'blocked':False}); assert r.status_code==200
        assert c.get('/api/me',headers=d).json()['blocked'] is False

def test_18_payment_requires_provider_and_cash():
    with client() as c:
        p=make_passenger(c,1150); d,did=make_driver(c,1151); c.post('/api/driver/online',headers=d)
        oid=c.post('/api/orders',headers=p,json={'pickup_lat':1,'pickup_lng':2,'pickup_address':'A','destination_lat':3,'destination_lng':4,'destination_address':'B'}).json()['order_id']; off=c.post('/api/offers',headers=d,json={'order_id':oid,'price':250,'eta_min':5}).json()['offer_id']; c.post(f'/api/offers/{off}/accept',headers=p)
        for st in ('DRIVER_ARRIVED','TRIP_STARTED','COMPLETED'): c.post(f'/api/orders/{oid}/status',headers=d,json={'status':st})
        assert c.post(f'/api/orders/{oid}/payment',headers=p,json={'method':'CASH'}).status_code==200
        assert c.get('/api/orders/'+str(oid),headers=p).json()['payment_status']=='CASH'

def test_19_frontend_map_source_and_admin_controls():
    with client() as c:
        html=c.get('/').text
        assert 'leaflet@1.9.4' in html
        assert 'tile.openstreetmap.org' in html
        assert 'OpenStreetMap contributors' in html
        assert 'Разблокировать + обнулить долг' in html

def test_20_sql_schema_all_tables_and_health_metadata():
    with client() as c:
        h=c.get('/api/health').json(); assert h['database']=='sqlite'
        with appmod.engine.connect() as db:
            tables={r[0] for r in db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        expected={'users','orders','offers','driver_locations','trips','messages','ratings','payments','commission_payments','admin_actions'}
        assert expected <= tables
