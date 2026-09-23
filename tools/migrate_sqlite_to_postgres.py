"""One-time migration helper.

Usage:
  SOURCE_SQLITE_URL=sqlite:////path/taxi.db \
  DATABASE_URL='postgresql+psycopg://...' \
  python tools/migrate_sqlite_to_postgres.py

The destination schema must already exist. Rows are copied in dependency order and
primary keys are preserved. This script intentionally does not copy Supabase Storage;
profile photos should be re-uploaded or copied separately.
"""
import os
from sqlalchemy import create_engine, text

source=os.environ['SOURCE_SQLITE_URL']
dest=os.environ['DATABASE_URL']
if not dest.startswith('postgresql'):
    raise SystemExit('DATABASE_URL must be PostgreSQL')
se=create_engine(source)
de=create_engine(dest,pool_pre_ping=True,connect_args={'sslmode':'require'})
order=['users','orders','offers','driver_locations','trips','messages','ratings','payments','commission_payments','admin_actions']
with se.connect() as s, de.begin() as d:
    for table in order:
        rows=s.execute(text(f'SELECT * FROM {table}')).mappings().all()
        if not rows: continue
        cols=list(rows[0].keys())
        placeholders=','.join(':'+c for c in cols)
        sql=text(f'INSERT INTO {table} ({",".join(cols)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING')
        d.execute(sql,[dict(r) for r in rows])
        print(table,len(rows))
print('Migration finished. Verify row counts before switching Render.')
