import requests
import time

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0',
    'Referer':    'https://www.nseindia.com',
    'Accept':     'application/json',
})

session.get('https://www.nseindia.com', timeout=10)
time.sleep(2)

r = session.get(
    'https://www.nseindia.com/api/'
    'snapshot-capital-market-largedeal',
    timeout=15
)

data = r.json()

# inspect each section properly
for key in ['BULK_DEALS', 'BLOCK_DEALS', 'SHORT_DEALS']:
    records = data[key]
    print(f"\n── {key}")
    print(f"   count     : {len(records)}")
    print(f"   item type : {type(records[0])}")
    print(f"   raw[0]    : {records[0]}")
    print(f"   raw[1]    : {records[1] if len(records)>1 else 'only 1 item'}")

# also check _DATA
for key in ['BULK_DEALS_DATA', 'BLOCK_DEALS_DATA', 'SHORT_DEALS_DATA']:
    print(f"\n── {key}")
    print(f"   type : {type(data[key])}")
    print(f"   value: {str(data[key])[:300]}")