from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
import sqlite3, os, math, time

DB_PATH = os.getenv("DB_PATH", "taxi.db")
app = FastAPI(title="Taxi Telegram Mini App", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, role TEXT, name TEXT, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS drivers(id TEXT PRIMARY KEY, name TEXT, phone TEXT, car_make TEXT NOT NULL, car_model TEXT NOT NULL, car_year INTEGER, plate TEXT, photo_url TEXT, rating REAL DEFAULT 5.0, reviews_count INTEGER DEFAULT 0, active INTEGER DEFAULT 1, debt REAL DEFAULT 0, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS rides(id INTEGER PRIMARY KEY AUTOINCREMENT, passenger_id TEXT, from_text TEXT, from_lat REAL, from_lon REAL, to_text TEXT, to_lat REAL, to_lon REAL, status TEXT, selected_driver_id TEXT, selected_price REAL, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS offers(id INTEGER PRIMARY KEY AUTOINCREMENT, ride_id INTEGER, driver_id TEXT, price REAL, eta_min INTEGER, status TEXT DEFAULT 'pending', UNIQUE(ride_id, driver_id));
    CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, ride_id INTEGER, driver_id TEXT, passenger_id TEXT, stars INTEGER, comment TEXT, created_at INTEGER);
    ''')
    # Demo drivers make the first test immediately usable. They are clearly marked in UI.
    demos=[
      ("demo1","Алан","+7 900 000-01-01","Toyota","Camry",2019,"А001АА",5.0,128,1,0),
      ("demo2","Тимур","+7 900 000-02-02","Lada","Vesta",2021,"А002АА",4.8,76,1,0),
      ("demo3","Руслан","+7 900 000-03-03","Hyundai","Solaris",2020,"А003АА",4.9,214,1,0)
    ]
    for d in demos:
        c.execute('''INSERT OR IGNORE INTO drivers(id,name,phone,car_make,car_model,car_year,plate,rating,reviews_count,active,debt,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (*d, int(time.time())))
    c.commit(); c.close()
init_db()

class UserIn(BaseModel):
    id: str
    role: str
    name: str = ""

class DriverIn(BaseModel):
    id: str
    name: str = Field(min_length=1)
    phone: str = ""
    car_make: str = Field(min_length=1)
    car_model: str = Field(min_length=1)
    car_year: Optional[int] = None
    plate: str = ""
    photo_url: str = ""

class RideIn(BaseModel):
    passenger_id: str
    from_text: str = ""
    from_lat: float
    from_lon: float
    to_text: str = ""
    to_lat: float
    to_lon: float

class OfferIn(BaseModel):
    driver_id: str
    price: float = Field(ge=100)
    eta_min: int = Field(ge=1, le=180)

class SelectIn(BaseModel):
    driver_id: str

class ReviewIn(BaseModel):
    passenger_id: str
    stars: int = Field(ge=1, le=5)
    comment: str = ""

@app.get("/health")
def health(): return {"ok": True, "version": "3.0"}

@app.post("/api/users")
def save_user(x: UserIn):
    if x.role not in ("passenger","driver"): raise HTTPException(400,"invalid role")
    c=db(); c.execute("INSERT INTO users(id,role,name,created_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET role=excluded.role,name=excluded.name", (x.id,x.role,x.name,int(time.time()))); c.commit(); c.close(); return {"ok":True}

@app.post("/api/drivers")
def register_driver(x: DriverIn):
    c=db(); c.execute('''INSERT INTO drivers(id,name,phone,car_make,car_model,car_year,plate,photo_url,created_at)
      VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,phone=excluded.phone,car_make=excluded.car_make,car_model=excluded.car_model,car_year=excluded.car_year,plate=excluded.plate,photo_url=excluded.photo_url''',
      (x.id,x.name,x.phone,x.car_make,x.car_model,x.car_year,x.plate,x.photo_url,int(time.time())))
    c.execute("INSERT INTO users(id,role,name,created_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET role='driver',name=excluded.name",(x.id,'driver',x.name,int(time.time())))
    c.commit(); row=c.execute("SELECT * FROM drivers WHERE id=?",(x.id,)).fetchone(); c.close(); return dict(row)

@app.get("/api/drivers")
def drivers():
    c=db(); rows=c.execute("SELECT * FROM drivers WHERE active=1 ORDER BY rating DESC, reviews_count DESC").fetchall(); c.close(); return [dict(r) for r in rows]

@app.get("/api/drivers/{driver_id}")
def driver(driver_id:str):
    c=db(); r=c.execute("SELECT * FROM drivers WHERE id=?",(driver_id,)).fetchone(); c.close()
    if not r: raise HTTPException(404,"driver not found")
    return dict(r)

@app.post("/api/rides")
def create_ride(x: RideIn):
    c=db(); cur=c.execute('''INSERT INTO rides(passenger_id,from_text,from_lat,from_lon,to_text,to_lat,to_lon,status,created_at)
       VALUES(?,?,?,?,?,?,?,?,?)''',(x.passenger_id,x.from_text,x.from_lat,x.from_lon,x.to_text,x.to_lat,x.to_lon,'offers',int(time.time())))
    ride_id=cur.lastrowid
    ds=c.execute("SELECT id,rating FROM drivers WHERE active=1 AND debt<1000 ORDER BY rating DESC").fetchall()
    # Demo marketplace: active drivers publish offers automatically. Real drivers will use /offers later.
    base=[100,105,120]
    for i,d in enumerate(ds[:3]):
        price=base[i] if str(d['id']).startswith('demo') else 100
        eta=5+i*3
        c.execute("INSERT OR IGNORE INTO offers(ride_id,driver_id,price,eta_min) VALUES(?,?,?,?)",(ride_id,d['id'],price,eta))
    c.commit(); c.close(); return {"ride_id":ride_id,"status":"offers"}

@app.get("/api/rides/{ride_id}/offers")
def ride_offers(ride_id:int):
    c=db(); rows=c.execute('''SELECT o.id,o.ride_id,o.driver_id,o.price,o.eta_min,o.status,d.name,d.car_make,d.car_model,d.car_year,d.plate,d.photo_url,d.rating,d.reviews_count
      FROM offers o JOIN drivers d ON d.id=o.driver_id WHERE o.ride_id=? ORDER BY o.price ASC, o.eta_min ASC''',(ride_id,)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.post("/api/rides/{ride_id}/select")
def select_driver(ride_id:int,x:SelectIn):
    c=db(); offer=c.execute("SELECT * FROM offers WHERE ride_id=? AND driver_id=?",(ride_id,x.driver_id)).fetchone()
    if not offer: c.close(); raise HTTPException(404,"offer not found")
    c.execute("UPDATE rides SET status='accepted',selected_driver_id=?,selected_price=? WHERE id=?",(x.driver_id,offer['price'],ride_id))
    c.execute("UPDATE offers SET status=CASE WHEN driver_id=? THEN 'accepted' ELSE 'rejected' END WHERE ride_id=?",(x.driver_id,ride_id)); c.commit(); c.close()
    return {"ok":True,"price":offer['price']}

@app.post("/api/rides/{ride_id}/offers")
def driver_offer(ride_id:int,x:OfferIn):
    c=db(); d=c.execute("SELECT * FROM drivers WHERE id=? AND active=1 AND debt<1000",(x.driver_id,)).fetchone()
    if not d: c.close(); raise HTTPException(403,"driver unavailable")
    try: c.execute("INSERT INTO offers(ride_id,driver_id,price,eta_min) VALUES(?,?,?,?)",(ride_id,x.driver_id,x.price,x.eta_min)); c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409,"offer already exists")
    c.close(); return {"ok":True}

@app.get("/api/rides/driver/{driver_id}")
def driver_rides(driver_id:str):
    c=db(); rows=c.execute('''SELECT r.*,o.price,o.eta_min FROM rides r JOIN offers o ON o.ride_id=r.id WHERE o.driver_id=? ORDER BY r.id DESC''',(driver_id,)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.post("/api/rides/{ride_id}/review")
def review(ride_id:int,x:ReviewIn):
    c=db(); r=c.execute("SELECT selected_driver_id,status FROM rides WHERE id=? AND passenger_id=?",(ride_id,x.passenger_id)).fetchone()
    if not r or r['status']!='accepted': c.close(); raise HTTPException(400,"ride not completed/selected")
    try: c.execute("INSERT INTO reviews(ride_id,driver_id,passenger_id,stars,comment,created_at) VALUES(?,?,?,?,?,?)",(ride_id,r['selected_driver_id'],x.passenger_id,x.stars,x.comment,int(time.time())))
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409,"review already exists")
    avg=c.execute("SELECT AVG(stars),COUNT(*) FROM reviews WHERE driver_id=?",(r['selected_driver_id'],)).fetchone()
    c.execute("UPDATE drivers SET rating=?,reviews_count=? WHERE id=?",(round(avg[0],2),avg[1],r['selected_driver_id'])); c.commit(); c.close(); return {"ok":True}
