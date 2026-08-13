# mongo-jira-check-assortment

เช็คตั๋ว Jira "POS Assortment : `<barcode>` | `<storeCodes>` (`<date>`)" (project `SUP`, label `PS_Front`) ที่ยัง
ไม่ปิด — ดึง `Barcode` + `Store Codes` (ร้านได้หลายร้านในตั๋วเดียว) จากตารางใน description แล้วรัน check เดิมที่ทีม
ใช้เช็คมือทีละร้าน (`store.stores`: หา `no` จาก `code` → `store.pos_assortments`: หา record ที่ `storeNo`+`barcode`
ตรงกัน) ถ้า**ทุกร้าน**ใน ticket sync ครบแล้วปิดตั๋วอัตโนมัติ ถ้ายังมีร้านไหนไม่ sync ติด flag ไว้ให้รัน POS Repair
ซ้ำ (ลิงก์ Jenkins มีอยู่ในตั๋วอยู่แล้ว) — เป็นหนึ่งโมดูลใน [Front Automation Hub](../README.md) คู่กับ
[mongo-jira-check-pointsum](../mongo-jira-check-pointsum/) / [mongo-jira-check-salesnote](../mongo-jira-check-salesnote/)
(ต่อ MongoDB เหมือนกันแต่คนละ collection) และ [rsp-sync-check](../rsp-sync-check/) (ที่มาของ pattern การย้ายตั๋ว
Open → In Progress → flag/Close ที่โมดูลนี้ใช้ต่อ)

## ทำไมต้องรันจากเครื่อง ไม่ใช่ในเบราว์เซอร์

ต่อ MongoDB ตรงจาก client-side JS ไม่ได้ (ไม่ใช่ HTTP, ต่อให้ทำได้ก็ไม่ควรฝัง connection string ไว้ในโค้ดที่ทุกคน
เปิด view source เห็นได้) และเรียก Jira REST API ตรงจาก browser ก็ไม่ได้เหมือนกัน (CORS + ต้องมี credential) —
งานจริงต้องรันจาก `mongo_jira_check.py` บนเครื่องผู้ใช้เท่านั้น

## Setup

1. `pip3 install pymongo`
2. Jira credential — ใช้ config เดียวกับ `rsp_sync_check.py` / `mongo-jira-check-pointsum` / `mongo-jira-check-salesnote`
   ได้เลยถ้าตั้งไว้แล้ว: env vars `JIRA_URL`/`JIRA_PERSONAL_TOKEN`, หรือ `~/.mongo_jira_check.json` /
   `~/.rsp_sync_check.json`
3. MongoDB connection string — ใช้ config เดียวกับโมดูลอื่นได้เลยถ้าตั้งไว้แล้ว (field `mongo_uri` เดียวกัน ต่อได้ทั้ง
   `membership` และ `store` DB ด้วย credential เดิม) ถ้ายังไม่มี ไปเอา connection string PROD (Local) แบบ read-only
   (`support_read_only`) จาก Confluence: **"Tooling Onboarding Checklist"** (space TOOK) → "Setup MongoDB Connection
   to PROD and NEST BETA" → แทน `{UserName}` ใน `appName` ด้วยชื่อตัวเอง แล้วเก็บไว้ที่ env var `MONGO_URI` หรือ field
   `"mongo_uri"` ใน `~/.mongo_jira_check.json` — **ห้าม commit connection string นี้ที่ไหนเด็ดขาด** (มี
   username/password ฝังอยู่)

```json
// ~/.mongo_jira_check.json
{
  "jira_url": "https://jira.tdshop.io/",
  "jira_token": "<your Personal Access Token>",
  "mongo_uri": "mongodb://support_read_only:<password>@tdshop-prod-shard-00-02.mvmwz.mongodb.net:27017,.../report?ssl=true&replicaSet=atlas-11cztn-shard-0&authSource=admin&readPreference=secondary&appName=<YourName>-mongo_jira_check"
}
```

## Running

```
python3 mongo_jira_check.py --dry-run   # ดูก่อนว่าจะทำอะไร ไม่แก้ตั๋วจริง
python3 mongo_jira_check.py             # รันจริง
```

เพิ่ม `--skip-update-check` ถ้าไม่อยากให้เช็คเวอร์ชันตอนเทส

## Logic

`search_open_assortment_tickets` (JQL: label `PS_Front` + summary มีคำว่า "POS Assortment" + ยังไม่ปิด) →
`parse_ticket` (ดึง `Barcode` และ `Store Codes` จากตาราง wiki-markup ใน description ด้วย regex — `Store Codes`
อาจมีได้หลายร้าน คั่นด้วยขึ้นบรรทัดใหม่/comma/space ในบล็อก code) → `check_ticket` วนเช็คทีละร้าน:

- `query_store_no` (`store.stores`, match ด้วย `code`) — หา `no` ของร้าน
- ถ้าไม่เจอร้าน → `status = store_not_found`, solution บอกให้เช็ค storeCode
- ถ้าเจอร้าน → `query_assortment` (`store.pos_assortments`, match ด้วย `storeNo` + `barcode`)
  - ไม่เจอ record → `status = not_synced`, solution บอกให้ export DB เช็คตาราง Assortment หรือรัน POS-Assortment
    บน Jenkins (ตรงกับ script เดิมที่ทีมใช้เช็คมือ)
  - เจอ record → `status = synced`

ผลของทุกร้านถูกใส่ในตาราง comment เดียวกัน (marker `Auto POS Assortment Check`) — โพสต์ซ้ำเฉพาะเมื่อผลลัพธ์
เปลี่ยนไปจากรอบก่อน (เทียบ `signature` ที่ฝังท้าย comment แต่ละอัน) ถ้า**ทุกร้าน** `status = synced`: assign ให้คนรัน
ตาม token (**เฉพาะถ้ายังไม่มี assignee เดิม**) → ถ้าตั๋วยัง **Open** ย้ายไป **In Progress** ก่อน แล้วปิดต่อ
(**Close**) ในรอบเดียวกัน พร้อม `resolution = "Won't Do"` และ `fixVersions = ["Won't Fix Release"]` (เอา flag ออก
ด้วยถ้าติดอยู่จากรอบก่อน) ถ้า**มีร้านใดร้านหนึ่ง** ยัง `not_synced`/`store_not_found` → ย้ายไป **In Progress** (ถ้า
ยังไม่ถูกย้าย) แล้วติด flag **`Impediment`** ไว้ (idempotent, เช็คซ้ำทุกรอบไม่ผูกกับจังหวะย้ายครั้งแรกเท่านั้น)

ทั้งหมดนี้เป็น movement pattern เดียวกับ `rsp_sync_check.py` / `mongo-jira-check-salesnote` เป๊ะ (Open → In Progress
ก่อนเสมอ, reconcile flag/Close จาก state ปัจจุบันทุกรอบ ไม่ใช่แค่ตอน transition edge) แค่เปลี่ยนตัวตัดสินใจเป็น "ทุก
ร้านใน ticket sync assortment ครบหรือยัง" (MongoDB, เช็คทีละร้าน) แทน docNo เดี่ยว หรือ point summary เดี่ยว

Transition ID ไม่ได้ hardcode ไว้ — `get_transition_id` เรียก `/issue/{key}/transitions` สดทุกครั้งแล้วหาด้วย
**ชื่อ** transition ("In Progress" / "Close") ถ้าหาไม่เจอ (เช่น workflow ถูกแก้ไปแล้ว) จะ print WARNING แล้วข้าม
ไม่ทำให้ script ทั้งรอบ crash — เหมือน `rsp_sync_check.py` เรื่อง flag (`set_flag`) ก็ใช้ endpoint เดียวกัน
(`POST /rest/greenhopper/1.0/xboard/issue/flag/flag.json`) เพราะเป็น board/customfield เดียวกัน

ไม่มี Jira token หรือ Mongo connection string ฝังอยู่ในไฟล์นี้เลยไม่ว่ากรณีไหน ปลอดภัยที่จะ commit ขึ้น public repo
เพราะ credential จริงอยู่แค่บนเครื่องผู้ใช้เท่านั้น เรียก Jira ผ่าน `curl` (ไม่ใช่ Python `urllib`) ด้วยเหตุผลเดียวกับ
`rsp_sync_check.py` (certifi bundle ไม่มี CA ขององค์กร)
