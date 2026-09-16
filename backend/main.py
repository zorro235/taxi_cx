from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from uuid import uuid4
import sqlite3, os
app=FastAPI(title="Taxi Telegram Mini App v2")
DB=os.getenv("TAXI_DB","taxi.db"); COMMISSION=0.10; MIN_PRICE=100
def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
with db() as c:
 c.executescript("""CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,telegram_id TEXT UNIQUE,role TEXT,name TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS drivers(user_id TEXT PRIMARY KEY,car_make TEXT NOT NULL,car_model TEXT NOT NULL,car_photo_url TEXT,rating REAL DEFAULT 5,rating_count INTEGER DEFAULT 0,debt REAL DEFAULT 0,active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS rides(id TEXT PRIMARY KEY,passenger_id TEXT,driver_id TEXT,pickup TEXT,destination TEXT,price REAL,commission REAL,status TEXT,created_at TEXT,completed_at TEXT);
CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,ride_id TEXT,passenger_id TEXT,driver_id TEXT,stars INTEGER,comment TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY,driver_id TEXT,amount REAL,provider TEXT,external_id TEXT,status TEXT,created_at TEXT);""")
class Driver(BaseModel):
 telegram_id:str; name:str=""; car_make:str=Field(min_length=1); car_model:str=Field(min_length=1); car_photo_url:Optional[str]=None
class Ride(BaseModel):
 passenger_telegram_id:str; pickup:str; destination:str; price:float=Field(ge=100)
class Review(BaseModel):
 passenger_telegram_id:str; stars:int=Field(ge=1,le=5); comment:str=""
class Payment(BaseModel):
 driver_telegram_id:str; amount:float=Field(gt=0); provider:str="SBP"; external_id:Optional[str]=None
def user(tg,role,name=""):
 with db() as c:
  x=c.execute("SELECT * FROM users WHERE telegram_id=?",(tg,)).fetchone()
  if x:return x["id"]
  i=str(uuid4());c.execute("INSERT INTO users VALUES(?,?,?,?,?)",(i,tg,role,name,datetime.utcnow().isoformat()));c.commit();return i
@app.get("/api/health")
def health():return {"ok":True,"version":"2.0"}
@app.post("/api/drivers/profile")
def profile(x:Driver):
 i=user(x.telegram_id,"driver",x.name)
 with db() as c:c.execute("""INSERT INTO drivers(user_id,car_make,car_model,car_photo_url) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET car_make=excluded.car_make,car_model=excluded.car_model,car_photo_url=excluded.car_photo_url""",(i,x.car_make,x.car_model,x.car_photo_url));c.commit()
 return {"ok":True,"driver_id":i}
@app.get("/api/drivers")
def drivers():
 with db() as c:r=c.execute("""SELECT u.id,u.name,d.car_make,d.car_model,d.car_photo_url,d.rating,d.rating_count,d.debt,d.active FROM drivers d JOIN users u ON u.id=d.user_id ORDER BY d.rating DESC""").fetchall()
 return [dict(x) for x in r]
@app.get("/api/drivers/{i}")
def driver(i):
 with db() as c:
  d=c.execute("SELECT u.id,u.name,d.* FROM drivers d JOIN users u ON u.id=d.user_id WHERE u.id=?",(i,)).fetchone()
  if not d:raise HTTPException(404,"Водитель не найден")
  rv=c.execute("SELECT stars,comment,created_at FROM reviews WHERE driver_id=? ORDER BY created_at DESC",(i,)).fetchall()
  z=dict(d);z["reviews"]=[dict(x) for x in rv];return z
@app.post("/api/rides")
def ride(x:Ride):
 pid=user(x.passenger_telegram_id,"passenger");rid=str(uuid4());comm=round(x.price*COMMISSION,2)
 with db() as c:c.execute("INSERT INTO rides VALUES(?,?,?,?,?,?,?,?,?,?)",(rid,pid,None,x.pickup,x.destination,x.price,comm,"searching",datetime.utcnow().isoformat(),None));c.commit()
 return {"ride_id":rid,"price":x.price,"commission":comm,"status":"searching"}
@app.post("/api/rides/{rid}/assign/{did}")
def assign(rid,did):
 with db() as c:
  d=c.execute("SELECT * FROM drivers WHERE user_id=?",(did,)).fetchone()
  if not d:raise HTTPException(404,"Водитель не найден")
  if not d["active"] or d["debt"]>=100:raise HTTPException(403,"Водитель заблокирован до оплаты задолженности")
  c.execute("UPDATE rides SET driver_id=?,status='accepted' WHERE id=?",(did,rid));c.commit()
 return {"ok":True}
@app.post("/api/rides/{rid}/complete")
def complete(rid):
 with db() as c:
  r=c.execute("SELECT * FROM rides WHERE id=?",(rid,)).fetchone()
  if not r or not r["driver_id"]:raise HTTPException(404,"Поездка/водитель не найдены")
  c.execute("UPDATE rides SET status='completed',completed_at=? WHERE id=?",(datetime.utcnow().isoformat(),rid))
  c.execute("UPDATE drivers SET debt=debt+? WHERE user_id=?",(r["commission"],r["driver_id"]));c.commit()
 return {"ok":True,"commission_added":r["commission"]}
@app.post("/api/rides/{rid}/review")
def review(rid,x:Review):
 with db() as c:
  r=c.execute("SELECT * FROM rides WHERE id=?",(rid,)).fetchone();p=user(x.passenger_telegram_id,"passenger")
  if not r or r["passenger_id"]!=p:raise HTTPException(403,"Нет доступа")
  c.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)",(str(uuid4()),rid,p,r["driver_id"],x.stars,x.comment,datetime.utcnow().isoformat()))
  a=c.execute("SELECT AVG(stars),COUNT(*) FROM reviews WHERE driver_id=?",(r["driver_id"],)).fetchone()
  c.execute("UPDATE drivers SET rating=?,rating_count=? WHERE user_id=?",(round(a[0],2),a[1],r["driver_id"]));c.commit()
 return {"ok":True}
@app.get("/api/drivers/{i}/payment")
def payment(i):
 with db() as c:d=c.execute("SELECT debt,active FROM drivers WHERE user_id=?",(i,)).fetchone()
 if not d:raise HTTPException(404,"Водитель не найден")
 return {"debt":round(d["debt"],2),"active":bool(d["active"]),"message":"Оплатите задолженность через СБП" if d["debt"] else "Задолженности нет"}
@app.post("/api/payments")
def pay(x:Payment):
 with db() as c:
  u=c.execute("SELECT id FROM users WHERE telegram_id=?",(x.driver_telegram_id,)).fetchone()
  if not u:raise HTTPException(404,"Водитель не найден")
  d=c.execute("SELECT debt FROM drivers WHERE user_id=?",(u["id"],)).fetchone(); applied=min(x.amount,d["debt"]);debt=round(d["debt"]-applied,2)
  c.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?)",(str(uuid4()),u["id"],x.amount,x.provider,x.external_id,"confirmed",datetime.utcnow().isoformat()))
  c.execute("UPDATE drivers SET debt=?,active=? WHERE user_id=?",(debt,1 if debt<100 else 0,u["id"]));c.commit()
 return {"ok":True,"applied":applied,"debt":debt,"active":debt<100}
