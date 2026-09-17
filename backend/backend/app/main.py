import os, json, hashlib, hmac, math, uuid, asyncio, platform
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

DATABASE_URL=os.getenv('DATABASE_URL','postgresql+asyncpg://taxi:CHANGE_ME@db:5432/taxi')
BOT_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')
COMMISSION_RATE=Decimal(os.getenv('COMMISSION_RATE','0.10'))
INIT_DATA_MAX_AGE=int(os.getenv('INIT_DATA_MAX_AGE','3600'))
ADMIN_TELEGRAM_IDS={int(x) for x in os.getenv('ADMIN_TELEGRAM_IDS','').split(',') if x.strip().isdigit()}
PUBLIC_BASE_URL=os.getenv('PUBLIC_BASE_URL','')
PAYMENT_PROVIDER_TOKEN=os.getenv('TELEGRAM_PAYMENT_PROVIDER_TOKEN','')
PAYMENT_CURRENCY=os.getenv('PAYMENT_CURRENCY','RUB')
TBANK_TERMINAL_KEY=os.getenv('TBANK_TERMINAL_KEY','')
TBANK_PASSWORD=os.getenv('TBANK_PASSWORD','')
TBANK_API_URL=os.getenv('TBANK_API_URL','https://securepay.tinkoff.ru/v2')
TBANK_WEBHOOK_SECRET=os.getenv('TBANK_WEBHOOK_SECRET','')
MEDIA_DIR=os.getenv('MEDIA_DIR','/app/media')
FRONTEND_DIR=os.getenv('FRONTEND_DIR','/app/frontend')
os.makedirs(MEDIA_DIR,exist_ok=True)
engine=create_async_engine(DATABASE_URL,pool_pre_ping=True,pool_recycle=1800)
Session=async_sessionmaker(engine,expire_on_commit=False)
app=FastAPI(title='Taxi Telegram Mini App',version='3.0.0')
app.add_middleware(CORSMiddleware,allow_origins=[x.strip() for x in os.getenv('CORS_ORIGINS','*').split(',') if x.strip()],allow_credentials=False,allow_methods=['GET','POST','OPTIONS'],allow_headers=['Content-Type','X-Telegram-Init-Data'])

@app.middleware('http')
async def api_prefix(request, call_next):
    if request.scope.get('path','').startswith('/api/'):
        request.scope['path']=request.scope['path'][4:]
    return await call_next(request)
ACTIVE={'SEARCHING_DRIVER','DRIVER_ASSIGNED','DRIVER_ARRIVED','TRIP_STARTED'}

def money(v): return Decimal(str(v)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
def valid_coords(lat,lng): return math.isfinite(lat) and math.isfinite(lng) and -90<=lat<=90 and -180<=lng<=180

def tbank_token(data:dict):
    # T-Bank internet-acquiring token: top-level scalar fields + Password, sorted by key.
    vals={k:v for k,v in data.items() if k!='Token' and not isinstance(v,(dict,list)) and v is not None}
    vals['Password']=TBANK_PASSWORD
    raw=''.join(str(vals[k]) for k in sorted(vals))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

async def tbank_post(method:str, payload:dict):
    if not TBANK_TERMINAL_KEY or not TBANK_PASSWORD:
        raise HTTPException(503,'Т-Банк не подключён: нужны TBANK_TERMINAL_KEY и TBANK_PASSWORD')
    body=dict(payload); body['TerminalKey']=TBANK_TERMINAL_KEY; body['Token']=tbank_token(body)
    async with httpx.AsyncClient(timeout=15) as client:
        r=await client.post(f'{TBANK_API_URL.rstrip("/")}/{method.lstrip("/")}',json=body)
    try: data=r.json()
    except Exception: data={'Success':False,'Message':r.text[:500]}
    if r.status_code>=400 or data.get('Success') is False:
        raise HTTPException(502,'Т-Банк: '+str(data.get('Message') or data.get('Details') or 'ошибка API'))
    return data

def verify_tbank_notification(data:dict):
    if not TBANK_PASSWORD: return False
    got=str(data.get('Token',''))
    vals={k:v for k,v in data.items() if k!='Token' and k not in ('Data','Receipt') and v is not None}
    vals['Password']=TBANK_PASSWORD
    raw=''.join(str(vals[k]) for k in sorted(vals))
    expected=hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return hmac.compare_digest(expected,got)


def validate_init_data(init_data:str)->dict:
    if not BOT_TOKEN or not init_data: raise HTTPException(401,'Telegram authorization is not configured')
    pairs=dict(parse_qsl(init_data,keep_blank_values=True)); received=pairs.pop('hash',None)
    if not received: raise HTTPException(401,'Missing Telegram hash')
    data='\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs))
    secret=hmac.new(b'WebAppData',BOT_TOKEN.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,data.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,received): raise HTTPException(401,'Invalid Telegram initData')
    try:
        auth_date=int(pairs.get('auth_date','0'))
        if auth_date<=0 or datetime.now(timezone.utc).timestamp()-auth_date>INIT_DATA_MAX_AGE: raise HTTPException(401,'Expired Telegram initData')
        tg=json.loads(pairs['user'])
        if not tg.get('id'): raise ValueError
    except (ValueError,TypeError,json.JSONDecodeError): raise HTTPException(401,'Invalid Telegram user data')
    return tg

async def current_user(init_data):
    tg=validate_init_data(init_data)
    async with Session() as s:
        row=(await s.execute(text('SELECT * FROM users WHERE telegram_id=:t'),{'t':tg['id']})).mappings().first()
        if not row:
            await s.execute(text("INSERT INTO users(telegram_id,name,username,photo_url) VALUES(:t,:n,:u,:p)"),{'t':tg['id'],'n':(tg.get('first_name','')+' '+tg.get('last_name','')).strip()[:120],'u':tg.get('username'),'p':tg.get('photo_url')})
            await s.commit(); row=(await s.execute(text('SELECT * FROM users WHERE telegram_id=:t'),{'t':tg['id']})).mappings().first()
        return row,tg
async def require_user(init_data): return (await current_user(init_data))[0]
async def require_admin(init_data):
    u=await require_user(init_data)
    if u['telegram_id'] not in ADMIN_TELEGRAM_IDS: raise HTTPException(403,'Developer access denied')
    return u

async def init_db():
    async with engine.begin() as c:
        await c.execute(text('''
CREATE TABLE IF NOT EXISTS users(
 id BIGSERIAL PRIMARY KEY, telegram_id BIGINT UNIQUE NOT NULL, role TEXT CHECK(role IN ('passenger','driver')),
 driver_status TEXT NOT NULL DEFAULT 'NONE' CHECK(driver_status IN ('NONE','PENDING','APPROVED','REJECTED')),
 name TEXT NOT NULL DEFAULT '', username TEXT, photo_url TEXT, phone TEXT, about TEXT,
 rating NUMERIC(3,2) NOT NULL DEFAULT 5.00, reviews INT NOT NULL DEFAULT 0,
 car_make TEXT, car_model TEXT, car_plate TEXT, car_photo TEXT,
 online BOOLEAN NOT NULL DEFAULT FALSE, blocked BOOLEAN NOT NULL DEFAULT FALSE,
 commission_balance NUMERIC(12,2) NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS orders(
 id BIGSERIAL PRIMARY KEY, passenger_id BIGINT NOT NULL REFERENCES users(id), pickup_lat DOUBLE PRECISION NOT NULL,pickup_lng DOUBLE PRECISION NOT NULL,pickup_address TEXT NOT NULL,
 destination_lat DOUBLE PRECISION NOT NULL,destination_lng DOUBLE PRECISION NOT NULL,destination_address TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'SEARCHING_DRIVER',selected_offer_id BIGINT, payment_status TEXT NOT NULL DEFAULT 'UNPAID' CHECK(payment_status IN ('UNPAID','PENDING','PAID','CASH')),
 payment_method TEXT CHECK(payment_method IN ('ONLINE','CASH')), final_price NUMERIC(12,2), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS offers(
 id BIGSERIAL PRIMARY KEY,order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,driver_id BIGINT NOT NULL REFERENCES users(id),price NUMERIC(12,2) NOT NULL CHECK(price>=100),eta_min INT NOT NULL DEFAULT 10 CHECK(eta_min BETWEEN 1 AND 180),status TEXT NOT NULL DEFAULT 'ACTIVE',created_at TIMESTAMPTZ NOT NULL DEFAULT now(),UNIQUE(order_id,driver_id)
);
CREATE TABLE IF NOT EXISTS driver_locations(driver_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,lat DOUBLE PRECISION NOT NULL,lng DOUBLE PRECISION NOT NULL,speed DOUBLE PRECISION,course DOUBLE PRECISION,accuracy DOUBLE PRECISION,updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS trips(id BIGSERIAL PRIMARY KEY,order_id BIGINT UNIQUE NOT NULL REFERENCES orders(id) ON DELETE CASCADE,driver_id BIGINT NOT NULL REFERENCES users(id),final_price NUMERIC(12,2) NOT NULL,commission NUMERIC(12,2) NOT NULL,started_at TIMESTAMPTZ,completed_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS messages(id BIGSERIAL PRIMARY KEY,order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,sender_id BIGINT NOT NULL REFERENCES users(id),body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 1000),created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ratings(id BIGSERIAL PRIMARY KEY,order_id BIGINT UNIQUE NOT NULL REFERENCES orders(id) ON DELETE CASCADE,passenger_id BIGINT NOT NULL REFERENCES users(id),driver_id BIGINT NOT NULL REFERENCES users(id),stars INT NOT NULL CHECK(stars BETWEEN 1 AND 5),comment TEXT CHECK(length(comment)<=500),created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS payments(id BIGSERIAL PRIMARY KEY,order_id BIGINT UNIQUE NOT NULL REFERENCES orders(id) ON DELETE CASCADE,passenger_id BIGINT NOT NULL REFERENCES users(id),amount NUMERIC(12,2) NOT NULL,payload TEXT UNIQUE NOT NULL,telegram_charge_id TEXT UNIQUE,payment_provider_charge_id TEXT,status TEXT NOT NULL DEFAULT 'PENDING',invoice_url TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),paid_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS commission_payments(id BIGSERIAL PRIMARY KEY,driver_id BIGINT NOT NULL REFERENCES users(id),amount NUMERIC(12,2) NOT NULL CHECK(amount>0),period_start DATE NOT NULL,period_end DATE NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',confirmed_at TIMESTAMPTZ,provider TEXT,provider_payment_id TEXT,provider_order_id TEXT UNIQUE,sbp_bank_id TEXT,sbp_bank_name TEXT,payment_url TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),paid_at TIMESTAMPTZ,provider_payload JSONB,UNIQUE(driver_id,period_start,period_end));
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS provider_payment_id TEXT;
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS provider_order_id TEXT;
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS sbp_bank_id TEXT;
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS sbp_bank_name TEXT;
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS payment_url TEXT;
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ;
ALTER TABLE commission_payments ADD COLUMN IF NOT EXISTS provider_payload JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS ux_commission_provider_payment ON commission_payments(provider,provider_payment_id) WHERE provider_payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status,created_at);CREATE INDEX IF NOT EXISTS idx_offers_order_status ON offers(order_id,status);CREATE INDEX IF NOT EXISTS idx_messages_order_created ON messages(order_id,created_at);CREATE INDEX IF NOT EXISTS idx_locations_updated ON driver_locations(updated_at);CREATE INDEX IF NOT EXISTS idx_users_driver_online ON users(role,driver_status,online);
'''))

async def weekly_commission_cycle():
    while True:
        try:
            now=datetime.now(timezone.utc)
            if now.weekday()==0 and now.hour==0 and now.minute<20:
                end=now.date()-timedelta(days=1); start=end-timedelta(days=6)
                async with Session() as s:
                    rows=(await s.execute(text("SELECT id,commission_balance FROM users WHERE role='driver' AND driver_status='APPROVED' AND commission_balance>0"))).mappings().all()
                    for r in rows:
                        await s.execute(text("INSERT INTO commission_payments(driver_id,amount,period_start,period_end,status) VALUES(:d,:a,:s,:e,'PENDING') ON CONFLICT(driver_id,period_start,period_end) DO NOTHING"),{'d':r['id'],'a':r['commission_balance'],'s':start,'e':end})
                        await s.execute(text("UPDATE users SET blocked=true,online=false WHERE id=:d AND commission_balance>0"),{'d':r['id']})
                    await s.commit()
        except Exception:
            pass
        await asyncio.sleep(60)

@app.on_event('startup')
async def startup():
    await init_db()
    asyncio.create_task(weekly_commission_cycle())
    if BOT_TOKEN and PUBLIC_BASE_URL:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(f'https://api.telegram.org/bot{BOT_TOKEN}/setWebhook', data={'url': PUBLIC_BASE_URL.rstrip('/')+'/api/telegram/webhook'})
        except Exception:
            pass

class RoleIn(BaseModel): role:str
class OrderIn(BaseModel):
 pickup_lat:float;pickup_lng:float;pickup_address:str=Field(min_length=1,max_length=300);destination_lat:float;destination_lng:float;destination_address:str=Field(min_length=1,max_length=300)
class OfferIn(BaseModel): order_id:int=Field(gt=0);price:Decimal=Field(ge=Decimal('100'),max_digits=12,decimal_places=2);eta_min:int=Field(default=10,ge=1,le=180)
class LocationIn(BaseModel): lat:float;lng:float;speed:Optional[float]=None;course:Optional[float]=None;accuracy:Optional[float]=None
class StatusIn(BaseModel): status:str
class ProfileIn(BaseModel): name:str=Field(min_length=1,max_length=120);phone:Optional[str]=Field(default=None,max_length=40);about:Optional[str]=Field(default=None,max_length=500);car_make:Optional[str]=Field(default=None,max_length=80);car_model:Optional[str]=Field(default=None,max_length=80);car_plate:Optional[str]=Field(default=None,max_length=30)
class MessageIn(BaseModel): body:str=Field(min_length=1,max_length=1000)
class RatingIn(BaseModel): stars:int=Field(ge=1,le=5);comment:Optional[str]=Field(default=None,max_length=500)
class PaymentMethodIn(BaseModel): method:str
class DriverDecision(BaseModel): approved:bool

@app.get('/health')
async def health(): return {'ok':True,'version':'3.0.0'}
@app.get('/reverse-geocode')
async def reverse_geocode(lat:float,lng:float,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_user(x_telegram_init_data)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(422,'Invalid coordinates')
    try:
        async with httpx.AsyncClient(timeout=8,headers={'User-Agent':'TaxiTelegramMiniApp/4.0'}) as client:
            r=await client.get('https://nominatim.openstreetmap.org/reverse',params={'format':'jsonv2','lat':lat,'lon':lng,'zoom':18,'addressdetails':1,'accept-language':'ru'})
        r.raise_for_status()
        data=r.json()
    except Exception:
        raise HTTPException(502,'Geocoding service unavailable')
    address=(data.get('display_name') or '').strip()
    if not address:
        raise HTTPException(404,'Адрес для этой точки не найден')
    return {'address':address,'lat':lat,'lng':lng}

@app.get('/geocode')
async def geocode(q:str,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_user(x_telegram_init_data)
    q=q.strip()
    if not q or len(q)>300: raise HTTPException(422,'Invalid address')
    try:
        async with httpx.AsyncClient(timeout=8,headers={'User-Agent':'TaxiTelegramMiniApp/4.0'}) as client:
            r=await client.get('https://nominatim.openstreetmap.org/search',params={'format':'jsonv2','limit':1,'accept-language':'ru','q':q})
        data=r.json()
    except Exception:
        raise HTTPException(502,'Geocoding service unavailable')
    if not data: raise HTTPException(404,'Адрес не найден')
    return {'lat':float(data[0]['lat']),'lng':float(data[0]['lon'])}

@app.get('/me')
async def me(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')): return dict(await require_user(x_telegram_init_data))
@app.post('/me/role')
async def role(body:RoleIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if body.role not in ('passenger','driver'): raise HTTPException(422,'Invalid role')
    async with Session() as s:
        if body.role=='driver':
            await s.execute(text("UPDATE users SET role='driver',driver_status=CASE WHEN driver_status='APPROVED' THEN 'APPROVED' ELSE 'PENDING' END WHERE id=:id"),{'id':u['id']})
        else: await s.execute(text("UPDATE users SET role='passenger' WHERE id=:id"),{'id':u['id']})
        await s.commit()
    return {'ok':True}
@app.post('/me/profile')
async def profile(body:ProfileIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        await s.execute(text('''UPDATE users SET name=:n,phone=:ph,about=:a,car_make=:mk,car_model=:mm,car_plate=:pl WHERE id=:id'''),{'n':body.name,'ph':body.phone,'a':body.about,'mk':body.car_make,'mm':body.car_model,'pl':body.car_plate,'id':u['id']});await s.commit()
    return {'ok':True}
@app.post('/me/photo')
async def photo(file:UploadFile=File(...),x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if file.content_type not in {'image/jpeg','image/png','image/webp'}: raise HTTPException(415,'Only JPEG, PNG or WEBP')
    data=await file.read()
    if len(data)>3*1024*1024: raise HTTPException(413,'Image is too large')
    ext={ 'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[file.content_type];name=f'{uuid.uuid4().hex}.{ext}';path=os.path.join(MEDIA_DIR,name);open(path,'wb').write(data)
    url=f'/media/{name}'
    async with Session() as s: await s.execute(text('UPDATE users SET photo_url=:p WHERE id=:id'),{'p':url,'id':u['id']});await s.commit()
    return {'photo_url':url}
@app.get('/media/{name}')
async def media(name:str):
    if os.path.basename(name)!=name: raise HTTPException(404,'Not found')
    path=os.path.join(MEDIA_DIR,name)
    if not os.path.isfile(path): raise HTTPException(404,'Not found')
    return FileResponse(path)

@app.post('/orders')
async def create_order(body:OrderIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='passenger': raise HTTPException(403,'Passenger role required')
    if not all(valid_coords(a,b) for a,b in [(body.pickup_lat,body.pickup_lng),(body.destination_lat,body.destination_lng)]): raise HTTPException(422,'Invalid coordinates')
    async with Session() as s:
        if (await s.execute(text("SELECT 1 FROM orders WHERE passenger_id=:p AND status IN ('SEARCHING_DRIVER','DRIVER_ASSIGNED','DRIVER_ARRIVED','TRIP_STARTED') LIMIT 1"),{'p':u['id']})).first(): raise HTTPException(409,'You already have an active order')
        r=await s.execute(text('''INSERT INTO orders(passenger_id,pickup_lat,pickup_lng,pickup_address,destination_lat,destination_lng,destination_address) VALUES(:p,:pl,:pg,:pa,:dl,:dg,:da) RETURNING id'''),{'p':u['id'],**body.model_dump()});oid=r.scalar();await s.commit()
    return {'order_id':oid}
@app.get('/orders/active')
async def active_orders(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='driver' or u['driver_status']!='APPROVED' or u['blocked'] or not u['online']: raise HTTPException(403,'Driver unavailable')
    async with Session() as s:
        r=await s.execute(text("SELECT id,pickup_lat,pickup_lng,pickup_address,destination_address,created_at FROM orders WHERE status='SEARCHING_DRIVER' ORDER BY created_at DESC LIMIT 30"));return [dict(x) for x in r.mappings().all()]
@app.get('/orders/mine/active')
async def mine(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text('''SELECT o.*,of.driver_id,of.price,of.eta_min,du.name driver_name,du.rating,du.reviews,du.car_make,du.car_model,du.photo_url driver_photo FROM orders o LEFT JOIN offers of ON of.id=o.selected_offer_id LEFT JOIN users du ON du.id=of.driver_id WHERE o.passenger_id=:p AND o.status IN ('SEARCHING_DRIVER','DRIVER_ASSIGNED','DRIVER_ARRIVED','TRIP_STARTED') ORDER BY o.id DESC LIMIT 1'''),{'p':u['id']});row=r.mappings().first();return dict(row) if row else None
@app.get('/orders/{oid}')
async def order_detail(oid:int,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text('''SELECT o.*,of.driver_id,of.price,of.eta_min,du.name driver_name,du.rating,du.reviews,du.car_make,du.car_model,du.photo_url driver_photo FROM orders o LEFT JOIN offers of ON of.id=o.selected_offer_id LEFT JOIN users du ON du.id=of.driver_id WHERE o.id=:o'''),{'o':oid});row=r.mappings().first()
        if not row: raise HTTPException(404,'Order not found')
        if u['id']!=row['passenger_id'] and u['id']!=row['driver_id']: raise HTTPException(403,'Not participant')
        return dict(row)
@app.get('/orders/{oid}/offers')
async def offers(oid:int,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        owner=(await s.execute(text('SELECT passenger_id,status FROM orders WHERE id=:o'),{'o':oid})).mappings().first()
        if not owner: raise HTTPException(404,'Order not found')
        if owner['passenger_id']!=u['id']: raise HTTPException(403,'Passenger only')
        r=await s.execute(text('''SELECT o.id,o.price,o.eta_min,o.driver_id,u.name,u.rating,u.reviews,u.car_make,u.car_model,u.photo_url FROM offers o JOIN users u ON u.id=o.driver_id WHERE o.order_id=:o AND o.status='ACTIVE' ORDER BY o.price,o.eta_min'''),{'o':oid});return [dict(x) for x in r.mappings().all()]
@app.post('/offers')
async def offer(body:OfferIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='driver' or u['driver_status']!='APPROVED' or u['blocked'] or not u['online']: raise HTTPException(403,'Driver unavailable')
    async with Session() as s:
        order=(await s.execute(text('SELECT id,status FROM orders WHERE id=:o FOR UPDATE'),{'o':body.order_id})).mappings().first()
        if not order or order['status']!='SEARCHING_DRIVER': raise HTTPException(409,'Order is no longer accepting offers')
        try:
            r=await s.execute(text('INSERT INTO offers(order_id,driver_id,price,eta_min) VALUES(:o,:d,:p,:e) RETURNING id'),{'o':body.order_id,'d':u['id'],'p':money(body.price),'e':body.eta_min})
        except Exception: await s.rollback();raise HTTPException(409,'You already offered a price')
        oid=r.scalar();await s.commit();return {'offer_id':oid}
@app.post('/offers/{offer_id}/accept')
async def accept(offer_id:int,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        off=(await s.execute(text('''SELECT of.id,of.order_id,of.driver_id,of.price,o.passenger_id,o.status order_status FROM offers of JOIN orders o ON o.id=of.order_id WHERE of.id=:id AND of.status='ACTIVE' FOR UPDATE OF of,o'''),{'id':offer_id})).mappings().first()
        if not off: raise HTTPException(404,'Offer not found')
        if off['passenger_id']!=u['id']: raise HTTPException(403,'Not your order')
        if off['order_status']!='SEARCHING_DRIVER': raise HTTPException(409,'Order already assigned')
        await s.execute(text("UPDATE offers SET status=CASE WHEN id=:id THEN 'ACCEPTED' ELSE 'REJECTED' END WHERE order_id=:o"),{'id':offer_id,'o':off['order_id']})
        await s.execute(text("UPDATE orders SET selected_offer_id=:id,status='DRIVER_ASSIGNED',final_price=:p,updated_at=now() WHERE id=:o"),{'id':offer_id,'p':off['price'],'o':off['order_id']});await s.commit()
    return {'ok':True,'driver_id':off['driver_id'],'price':float(off['price'])}

@app.post('/driver/online')
async def online(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='driver' or u['driver_status']!='APPROVED' or u['blocked']: raise HTTPException(403,'Driver unavailable')
    async with Session() as s:
        await s.execute(text('UPDATE users SET online=NOT online WHERE id=:id'),{'id':u['id']});await s.commit();r=(await s.execute(text('SELECT online FROM users WHERE id=:id'),{'id':u['id']})).mappings().first();return dict(r)
@app.post('/driver/location')
async def location(body:LocationIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='driver' or u['driver_status']!='APPROVED' or u['blocked']: raise HTTPException(403,'Driver unavailable')
    if not valid_coords(body.lat,body.lng): raise HTTPException(422,'Invalid coordinates')
    if body.accuracy is not None and (not math.isfinite(body.accuracy) or body.accuracy<0 or body.accuracy>5000): raise HTTPException(422,'Invalid accuracy')
    async with Session() as s:
        await s.execute(text('''INSERT INTO driver_locations(driver_id,lat,lng,speed,course,accuracy) VALUES(:d,:lat,:lng,:sp,:co,:ac) ON CONFLICT(driver_id) DO UPDATE SET lat=EXCLUDED.lat,lng=EXCLUDED.lng,speed=EXCLUDED.speed,course=EXCLUDED.course,accuracy=EXCLUDED.accuracy,updated_at=now()'''),{'d':u['id'],'lat':body.lat,'lng':body.lng,'sp':body.speed,'co':body.course,'ac':body.accuracy});await s.commit();return {'ok':True}
@app.get('/drivers/{driver_id}/location')
async def driver_location(driver_id:int,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        if u['id']!=driver_id:
            ok=(await s.execute(text("SELECT 1 FROM orders o JOIN offers of ON of.id=o.selected_offer_id WHERE o.passenger_id=:p AND of.driver_id=:d AND o.status IN ('DRIVER_ASSIGNED','DRIVER_ARRIVED','TRIP_STARTED') LIMIT 1"),{'p':u['id'],'d':driver_id})).first()
            if not ok: raise HTTPException(403,'Location access denied')
        r=await s.execute(text('SELECT lat,lng,speed,course,accuracy,updated_at FROM driver_locations WHERE driver_id=:d'),{'d':driver_id});row=r.mappings().first()
        if not row: raise HTTPException(404,'Location unavailable')
        if row['updated_at'] < datetime.now(timezone.utc)-timedelta(minutes=2): raise HTTPException(410,'Location is stale')
        return dict(row)

TRANS={
 'DRIVER_ASSIGNED':{'DRIVER_ARRIVED','CANCELLED'},'DRIVER_ARRIVED':{'TRIP_STARTED','CANCELLED'},'TRIP_STARTED':{'COMPLETED'},
 'SEARCHING_DRIVER':{'DRIVER_ASSIGNED','CANCELLED'},'COMPLETED':set(),'CANCELLED':set()
}
@app.post('/orders/{oid}/status')
async def status(oid:int,body:StatusIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text('SELECT o.*,of.driver_id,of.price FROM orders o LEFT JOIN offers of ON of.id=o.selected_offer_id WHERE o.id=:o FOR UPDATE'),{'o':oid});o=r.mappings().first()
        if not o: raise HTTPException(404,'Order not found')
        if body.status not in TRANS.get(o['status'],set()): raise HTTPException(409,'Invalid status transition')
        if body.status=='CANCELLED':
            if u['id']!=o['passenger_id'] and u['id']!=o['driver_id']: raise HTTPException(403,'Not participant')
        elif u['id']!=o['driver_id']: raise HTTPException(403,'Assigned driver only')
        if body.status=='TRIP_STARTED':
            price=o['final_price'] or o['price']; comm=money(price*COMMISSION_RATE)
            await s.execute(text("INSERT INTO trips(order_id,driver_id,final_price,commission,started_at) VALUES(:o,:d,:p,:c,now()) ON CONFLICT(order_id) DO UPDATE SET started_at=now(),final_price=EXCLUDED.final_price"),{'o':oid,'d':o['driver_id'],'p':price,'c':comm})
        if body.status=='COMPLETED':
            price=o['final_price'] or o['price'];comm=money(price*COMMISSION_RATE)
            await s.execute(text("INSERT INTO trips(order_id,driver_id,final_price,commission,started_at,completed_at) VALUES(:o,:d,:p,:c,COALESCE((SELECT started_at FROM trips WHERE order_id=:o),now()),now()) ON CONFLICT(order_id) DO UPDATE SET final_price=EXCLUDED.final_price,commission=EXCLUDED.commission,completed_at=now()"),{'o':oid,'d':o['driver_id'],'p':price,'c':comm})
            await s.execute(text('UPDATE users SET commission_balance=commission_balance+:c WHERE id=:d'),{'c':comm,'d':o['driver_id']})
        await s.execute(text('UPDATE orders SET status=:st,updated_at=now() WHERE id=:o'),{'st':body.status,'o':oid});await s.commit()
    return {'ok':True}

async def participant(s,oid,uid):
    r=await s.execute(text('SELECT passenger_id,(SELECT driver_id FROM offers WHERE id=selected_offer_id) driver_id FROM orders WHERE id=:o'),{'o':oid});o=r.mappings().first()
    if not o or uid not in (o['passenger_id'],o['driver_id']): raise HTTPException(403,'Not participant')
    return o
@app.get('/orders/{oid}/messages')
async def get_messages(oid:int,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        await participant(s,oid,u['id']);r=await s.execute(text('SELECT m.*,u.name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.order_id=:o ORDER BY m.id LIMIT 200'),{'o':oid});return [dict(x) for x in r.mappings().all()]
@app.post('/orders/{oid}/messages')
async def send_message(oid:int,body:MessageIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        o=await participant(s,oid,u['id']);
        if o['passenger_id']!=u['id'] and o['driver_id']!=u['id']: raise HTTPException(403,'Not participant')
        await s.execute(text('INSERT INTO messages(order_id,sender_id,body) VALUES(:o,:s,:b)'),{'o':oid,'s':u['id'],'b':body.body});await s.commit();return {'ok':True}
@app.post('/orders/{oid}/rating')
async def rating(oid:int,body:RatingIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        o=(await s.execute(text("SELECT passenger_id,(SELECT driver_id FROM offers WHERE id=selected_offer_id) driver_id,status FROM orders WHERE id=:o"),{'o':oid})).mappings().first()
        if not o or o['passenger_id']!=u['id'] or o['status']!='COMPLETED': raise HTTPException(403,'Rating unavailable')
        if (await s.execute(text('SELECT 1 FROM ratings WHERE order_id=:o'),{'o':oid})).first(): raise HTTPException(409,'Already rated')
        await s.execute(text('INSERT INTO ratings(order_id,passenger_id,driver_id,stars,comment) VALUES(:o,:p,:d,:s,:c)'),{'o':oid,'p':u['id'],'d':o['driver_id'],'s':body.stars,'c':body.comment})
        await s.execute(text('UPDATE users SET rating=((rating*reviews)+:s)/(reviews+1),reviews=reviews+1 WHERE id=:d'),{'s':body.stars,'d':o['driver_id']});await s.commit();return {'ok':True}


@app.get('/driver/orders/active')
async def driver_active(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='driver': raise HTTPException(403,'Driver only')
    async with Session() as s:
        r=await s.execute(text("SELECT o.*,of.driver_id,of.price,du.name passenger_name,du.photo_url passenger_photo,du.phone passenger_phone FROM orders o JOIN offers of ON of.id=o.selected_offer_id JOIN users du ON du.id=o.passenger_id WHERE of.driver_id=:d AND o.status IN ('DRIVER_ASSIGNED','DRIVER_ARRIVED','TRIP_STARTED') ORDER BY o.id DESC LIMIT 1"),{'d':u['id']})
        row=r.mappings().first(); return dict(row) if row else None

@app.get('/orders/mine/history')
async def mine_history(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text("SELECT o.id,o.status,o.final_price,o.payment_status,o.payment_method,o.created_at,o.updated_at,o.pickup_address,o.destination_address,d.id driver_id,d.name driver_name,d.photo_url driver_photo,d.rating rating FROM orders o LEFT JOIN offers of ON of.id=o.selected_offer_id LEFT JOIN users d ON d.id=of.driver_id WHERE o.passenger_id=:p AND o.status IN ('COMPLETED','CANCELLED') ORDER BY o.id DESC LIMIT 100"),{'p':u['id']})
        return [dict(x) for x in r.mappings().all()]

@app.get('/driver/finance')
async def finance(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='driver': raise HTTPException(403,'Driver only')
    async with Session() as s:
        p=(await s.execute(text('''SELECT id,amount,status,period_start,period_end,payment_url,sbp_bank_name,provider_payment_id,paid_at FROM commission_payments WHERE driver_id=:d ORDER BY id DESC LIMIT 1'''),{'d':u['id']})).mappings().first()
    return {'commission_balance':float(u['commission_balance']),'blocked':u['blocked'],'payment':dict(p) if p else None}

@app.get('/driver/commission/banks')
async def commission_banks(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='driver': raise HTTPException(403,'Driver only')
    data=await tbank_post('GetQrBankList',{'ScenarioType':'qr','Device':{'Type':'mobile','Os':platform.system() or 'Android'},'PaymentMethod':'SBP'})
    return {'banks':data.get('Data') or data.get('Banks') or []}

class CommissionPayIn(BaseModel):
    bank_id:str=Field(min_length=1,max_length=100)
    bank_name:Optional[str]=Field(default=None,max_length=200)

@app.post('/driver/commission/pay')
async def commission_pay(body:CommissionPayIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if u['role']!='driver': raise HTTPException(403,'Driver only')
    amount=money(u['commission_balance'])
    if amount<=0: return {'paid':True,'amount':0,'message':'Комиссии нет'}
    async with Session() as s:
        p=(await s.execute(text('''SELECT * FROM commission_payments WHERE driver_id=:d AND status IN ('PENDING','CREATED') ORDER BY id DESC LIMIT 1'''),{'d':u['id']})).mappings().first()
        if not p:
            now=datetime.now(timezone.utc).date(); start=now-timedelta(days=now.weekday()); end=start+timedelta(days=6)
            await s.execute(text('''INSERT INTO commission_payments(driver_id,amount,period_start,period_end,status) VALUES(:d,:a,:s,:e,'PENDING') ON CONFLICT(driver_id,period_start,period_end) DO NOTHING'''),{'d':u['id'],'a':amount,'s':start,'e':end})
            p=(await s.execute(text("SELECT * FROM commission_payments WHERE driver_id=:d AND status IN ('PENDING','CREATED') ORDER BY id DESC LIMIT 1"),{'d':u['id']})).mappings().first()
        order_id=f'COM-{p["id"]}-{u["id"]}-{uuid.uuid4().hex[:8]}'[:50]
        description=f'Комиссия такси ID {u["telegram_id"]}'[:140]
        payload={'Amount':int(amount*100),'OrderId':order_id,'Description':description,'PayType':'O','Language':'ru','Device':'Mobile','DeviceOs':platform.system() or 'Android','NotificationURL':PUBLIC_BASE_URL.rstrip('/')+'/api/tbank/notification' if PUBLIC_BASE_URL else ''}
        init=await tbank_post('Init',payload)
        payment_id=str(init.get('PaymentId') or '')
        if not payment_id: raise HTTPException(502,'Т-Банк не вернул PaymentId')
        qr=await tbank_post('GetQr',{'PaymentId':payment_id,'DataType':'PAYLOAD','BankId':body.bank_id,'PaymentMethod':'SBP'})
        url=qr.get('Data') or qr.get('PaymentURL') or init.get('PaymentURL')
        if not url: raise HTTPException(502,'Т-Банк не вернул ссылку СБП')
        await s.execute(text('''UPDATE commission_payments SET status='CREATED',provider='TBANK',provider_payment_id=:pid,provider_order_id=:oid,sbp_bank_id=:bid,sbp_bank_name=:bn,payment_url=:url WHERE id=:id'''),{'pid':payment_id,'oid':order_id,'bid':body.bank_id,'bn':body.bank_name,'url':url,'id':p['id']})
        await s.commit()
        return {'paid':False,'amount':float(amount),'payment_id':payment_id,'payment_url':url}

@app.post('/tbank/notification')
async def tbank_notification(request:Request):
    data=await request.json()
    if not verify_tbank_notification(data): raise HTTPException(401,'Invalid T-Bank notification token')
    status=str(data.get('Status','')).upper(); pid=str(data.get('PaymentId',''))
    if status not in ('CONFIRMED','COMPLETED'): return 'OK'
    async with Session() as s:
        async with s.begin():
            p=(await s.execute(text("SELECT * FROM commission_payments WHERE provider='TBANK' AND provider_payment_id=:pid FOR UPDATE"),{'pid':pid})).mappings().first()
            if not p or p['status']=='PAID': return 'OK'
            expected=int(money(p['amount'])*100); got=int(data.get('Amount') or 0)
            if expected!=got: raise HTTPException(400,'Payment amount mismatch')
            await s.execute(text("UPDATE commission_payments SET status='PAID',paid_at=now(),confirmed_at=now(),provider_payload=:payload WHERE id=:id"),{'id':p['id'],'payload':json.dumps(data,ensure_ascii=False)})
            await s.execute(text("UPDATE users SET commission_balance=GREATEST(commission_balance-:a,0),blocked=false WHERE id=:d"),{'a':p['amount'],'d':p['driver_id']})
    return 'OK'

# Payments for the physical taxi service use Telegram's Bot Payments with a third-party provider token.
@app.post('/orders/{oid}/payment')
async def payment(oid:int,body:PaymentMethodIn,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    u=await require_user(x_telegram_init_data)
    if body.method not in ('ONLINE','CASH'): raise HTTPException(422,'Invalid payment method')
    async with Session() as s:
        o=(await s.execute(text('SELECT * FROM orders WHERE id=:o'),{'o':oid})).mappings().first()
        if not o or o['passenger_id']!=u['id'] or o['status']!='COMPLETED': raise HTTPException(403,'Payment unavailable')
        amount=o['final_price']
        if not amount: raise HTTPException(409,'Final price missing')
        if body.method=='CASH': await s.execute(text("UPDATE orders SET payment_method='CASH',payment_status='CASH' WHERE id=:o"),{'o':oid});await s.commit();return {'method':'CASH','status':'CASH'}
        if not PAYMENT_PROVIDER_TOKEN: raise HTTPException(503,'Online payment provider is not configured')
        payload=f'taxi:{oid}:{uuid.uuid4().hex}'
        url='https://api.telegram.org/bot'+BOT_TOKEN+'/createInvoiceLink'
        data={'title':f'Такси #{oid}','description':f'Оплата поездки #{oid}','payload':payload,'provider_token':PAYMENT_PROVIDER_TOKEN,'currency':PAYMENT_CURRENCY,'prices':json.dumps([{'label':f'Поездка #{oid}','amount':int((amount*100).to_integral_value())}])}
        async with httpx.AsyncClient(timeout=10) as client:
            rr=await client.post(url,data=data);jj=rr.json()
        if not jj.get('ok'): raise HTTPException(502,'Telegram payment service error')
        invoice=jj['result'];await s.execute(text('INSERT INTO payments(order_id,passenger_id,amount,payload,status,invoice_url) VALUES(:o,:p,:a,:pl,\'PENDING\',:u) ON CONFLICT(order_id) DO UPDATE SET payload=EXCLUDED.payload,amount=EXCLUDED.amount,status=\'PENDING\',invoice_url=EXCLUDED.invoice_url'),{'o':oid,'p':u['id'],'a':amount,'pl':payload,'u':invoice});await s.execute(text("UPDATE orders SET payment_method='ONLINE',payment_status='PENDING' WHERE id=:o"),{'o':oid});await s.commit();return {'method':'ONLINE','status':'PENDING','invoice_url':invoice}

@app.post('/telegram/webhook')
async def telegram_webhook(request:Request):
    update=await request.json()
    pc=update.get('pre_checkout_query')
    if pc:
        payload=pc.get('invoice_payload'); ok=False; error='Payment cannot be processed'
        async with Session() as s:
            p=(await s.execute(text('SELECT * FROM payments WHERE payload=:p'),{'p':payload})).mappings().first()
            if p and p['status']=='PENDING' and int(pc.get('total_amount',0))==int((p['amount']*100).to_integral_value()) and pc.get('currency')==PAYMENT_CURRENCY:
                ok=True
        method='answerPreCheckoutQuery';url='https://api.telegram.org/bot'+BOT_TOKEN+'/'+method
        data={'pre_checkout_query_id':pc.get('id'),'ok':ok}
        if not ok:data['error_message']=error
        try:
            async with httpx.AsyncClient(timeout=8) as client: await client.post(url,data=data)
        except Exception: pass
        return {'ok':True}
    sp=update.get('message',{}).get('successful_payment')
    if sp:
        payload=sp.get('invoice_payload')
        async with Session() as s:
            p=(await s.execute(text('SELECT * FROM payments WHERE payload=:p FOR UPDATE'),{'p':payload})).mappings().first()
            if p and p['status']!='PAID':
                await s.execute(text("UPDATE payments SET status='PAID',telegram_charge_id=:tc,payment_provider_charge_id=:pc,paid_at=now() WHERE id=:id"),{'tc':sp.get('telegram_payment_charge_id'),'pc':sp.get('provider_payment_charge_id'),'id':p['id']})
                await s.execute(text("UPDATE orders SET payment_status='PAID' WHERE id=:o"),{'o':p['order_id']});await s.commit()
    return {'ok':True}

# Developer/admin dashboard. Access is tied to Telegram ID allow-list; no password is stored.
@app.get('/admin/commission')
async def admin_commission(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text('''SELECT c.id,c.driver_id,c.amount,c.period_start,c.period_end,c.status,c.confirmed_at,c.provider,c.provider_payment_id,c.sbp_bank_name,c.created_at,c.paid_at,u.telegram_id,u.name FROM commission_payments c JOIN users u ON u.id=c.driver_id ORDER BY c.period_start DESC,c.id DESC LIMIT 500'''))
        return [dict(x) for x in r.mappings().all()]

@app.post('/admin/commission/{payment_id}/confirm')
async def admin_confirm_commission(payment_id:int,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s:
        p=(await s.execute(text('SELECT * FROM commission_payments WHERE id=:id FOR UPDATE'),{'id':payment_id})).mappings().first()
        if not p: raise HTTPException(404,'Commission payment not found')
        if p['status']=='CONFIRMED': return {'ok':True}
        await s.execute(text("UPDATE commission_payments SET status='CONFIRMED',confirmed_at=now() WHERE id=:id"),{'id':payment_id})
        await s.execute(text('UPDATE users SET commission_balance=GREATEST(commission_balance-:a,0),blocked=false WHERE id=:d'),{'a':p['amount'],'d':p['driver_id']})
        await s.commit()
    return {'ok':True}

@app.get('/admin/summary')
async def admin_summary(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s:
        q=lambda sql: s.execute(text(sql))
        total=(await q("SELECT count(*) n FROM users WHERE role='driver' AND driver_status='APPROVED'")).scalar()
        online=(await q("SELECT count(*) n FROM users WHERE role='driver' AND driver_status='APPROVED' AND online=true AND blocked=false")).scalar()
        clients=(await q("SELECT count(*) n FROM users WHERE role='passenger'")).scalar()
        active=(await q("SELECT count(*) n FROM orders WHERE status IN ('SEARCHING_DRIVER','DRIVER_ASSIGNED','DRIVER_ARRIVED','TRIP_STARTED')")).scalar()
        completed=(await q("SELECT count(*) n FROM orders WHERE status='COMPLETED'")).scalar()
        revenue=(await q("SELECT COALESCE(sum(final_price),0) n FROM orders WHERE status='COMPLETED'")).scalar()
        return {'drivers_total':total,'drivers_online':online,'clients_total':clients,'active_orders':active,'completed_trips':completed,'completed_amount':float(revenue or 0)}
@app.get('/admin/drivers')
async def admin_drivers(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text('''SELECT u.id,u.telegram_id,u.name,u.username,u.photo_url,u.phone,u.about,u.rating,u.reviews,u.car_make,u.car_model,u.car_plate,u.online,u.blocked,u.driver_status,u.commission_balance,COUNT(t.id) trips_count FROM users u LEFT JOIN trips t ON t.driver_id=u.id WHERE u.role='driver' GROUP BY u.id ORDER BY u.created_at DESC'''));return [dict(x) for x in r.mappings().all()]
@app.get('/admin/drivers/{driver_id}/trips')
async def admin_driver_trips(driver_id:int,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text('''SELECT t.id,t.order_id,t.final_price,t.commission,t.started_at,t.completed_at,o.passenger_id,o.pickup_address,o.destination_address,o.pickup_lat,o.pickup_lng,o.destination_lat,o.destination_lng,u.name passenger_name,u.photo_url passenger_photo FROM trips t JOIN orders o ON o.id=t.order_id JOIN users u ON u.id=o.passenger_id WHERE t.driver_id=:d ORDER BY t.completed_at DESC'''),{'d':driver_id});return [dict(x) for x in r.mappings().all()]
@app.get('/admin/clients')
async def admin_clients(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text('''SELECT u.id,u.telegram_id,u.name,u.username,u.photo_url,u.phone,u.about,u.created_at,COUNT(o.id) trips_count FROM users u LEFT JOIN orders o ON o.passenger_id=u.id AND o.status='COMPLETED' WHERE u.role='passenger' GROUP BY u.id ORDER BY u.created_at DESC'''));return [dict(x) for x in r.mappings().all()]
@app.get('/admin/orders')
async def admin_orders(x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s:
        r=await s.execute(text('''SELECT o.id,o.status,o.final_price,o.payment_status,o.created_at,o.pickup_address,o.destination_address,p.name passenger_name,d.name driver_name,o.pickup_lat,o.pickup_lng,o.destination_lat,o.destination_lng FROM orders o JOIN users p ON p.id=o.passenger_id LEFT JOIN offers of ON of.id=o.selected_offer_id LEFT JOIN users d ON d.id=of.driver_id ORDER BY o.created_at DESC LIMIT 200'''));return [dict(x) for x in r.mappings().all()]
@app.post('/admin/drivers/{driver_id}/decision')
async def admin_driver_decision(driver_id:int,body:DriverDecision,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s:
        await s.execute(text("UPDATE users SET driver_status=:st,role='driver' WHERE id=:id"),{'st':'APPROVED' if body.approved else 'REJECTED','id':driver_id});await s.commit();return {'ok':True}
@app.post('/admin/drivers/{driver_id}/block')
async def admin_driver_block(driver_id:int,x_telegram_init_data:str=Header(default='',alias='X-Telegram-Init-Data')):
    await require_admin(x_telegram_init_data)
    async with Session() as s: await s.execute(text('UPDATE users SET blocked=NOT blocked,online=false WHERE id=:id'),{'id':driver_id});await s.commit();return {'ok':True}

if os.path.isdir(FRONTEND_DIR):
    app.mount('/', StaticFiles(directory=FRONTEND_DIR, html=True), name='frontend')
