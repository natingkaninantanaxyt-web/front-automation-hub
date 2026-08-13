# mongo-jira-check-salesnote

เช็คตั๋ว Jira "SalesNote : `<docNo>`-`<date>`" (project `SUP`, label `PS_Front`) ที่ยังไม่ปิด — ดึง `doc_no`
(เลข sale note เช่น `SN26-TH004759-M02-0000073`) จากตั๋วแล้วรัน aggregate เดิมที่ใช้เช็คมือ (`store.sale_notes`)
ถ้า `status = COMPLETED` ปิดตั๋วอัตโนมัติ ถ้ายังเป็น `status = NEW` ติด flag ไว้แล้วย้ายไป **In Progress** —
เป็นหนึ่งโมดูลใน [Front Automation Hub](../README.md) คู่กับ [mongo-jira-check-pointsum](../mongo-jira-check-pointsum/)
(ต่อ MongoDB เหมือนกันแต่คนละ collection) และ [rsp-sync-check](../rsp-sync-check/) (ที่มาของ pattern การย้ายตั๋ว
Open → In Progress → flag/Close ที่โมดูลนี้ใช้ต่อ)

## ทำไมต้องรันจากเครื่อง ไม่ใช่ในเบราว์เซอร์

ต่อ MongoDB ตรงจาก client-side JS ไม่ได้ (ไม่ใช่ HTTP, ต่อให้ทำได้ก็ไม่ควรฝัง connection string ไว้ในโค้ดที่ทุกคน
เปิด view source เห็นได้) และเรียก Jira REST API ตรงจาก browser ก็ไม่ได้เหมือนกัน (CORS + ต้องมี credential) —
งานจริงต้องรันจาก `mongo_jira_check_salesnote.py` บนเครื่องผู้ใช้เท่านั้น

## Setup

1. `pip3 install pymongo`
2. Jira credential — ใช้ config เดียวกับ `rsp_sync_check.py` / `mongo-jira-check-pointsum` ได้เลยถ้าตั้งไว้แล้ว: env
   vars `JIRA_URL`/`JIRA_PERSONAL_TOKEN`, หรือ `~/.mongo_jira_check.json` / `~/.rsp_sync_check.json`
3. MongoDB connection string — ใช้ config เดียวกับ `mongo-jira-check-pointsum` ได้เลยถ้าตั้งไว้แล้ว (field `mongo_uri`
   เดียวกัน ต่อได้ทั้ง `membership` และ `store` DB ด้วย credential เดิม) ถ้ายังไม่มี ไปเอา connection string PROD
   (Local) แบบ read-only (`support_read_only`) จาก Confluence: **"Tooling Onboarding Checklist"** (space TOOK) →
   "Setup MongoDB Connection to PROD and NEST BETA" → แทน `{UserName}` ใน `appName` ด้วยชื่อตัวเอง แล้วเก็บไว้ที่
   env var `MONGO_URI` หรือ field `"mongo_uri"` ใน `~/.mongo_jira_check.json` — **ห้าม commit connection string นี้
   ที่ไหนเด็ดขาด** (มี username/password ฝังอยู่)

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
python3 mongo_jira_check_salesnote.py --dry-run   # ดูก่อนว่าจะทำอะไร ไม่แก้ตั๋วจริง
python3 mongo_jira_check_salesnote.py             # รันจริง
```

เพิ่ม `--skip-update-check` ถ้าไม่อยากให้เช็คเวอร์ชันตอนเทส

## Logic

`search_open_sale_note_tickets` (JQL: label `PS_Front` + summary มีคำว่า "SalesNote" + ยังไม่ปิด) →
`parse_ticket` (ดึง `doc_no` = docNo ของ sale note จาก wiki-table ใน description ด้วย regex เดียวกับที่
`mongo-jira-check-pointsum` ใช้) → `query_sale_note` (aggregate เดิมจากสคริปต์ที่ทีมใช้เช็คมืออยู่แล้วบน
`store.sale_notes`, match ด้วย `docNo`) → ถ้าไม่เจอ record ใน Mongo เลย ปล่อยตั๋วไว้เฉยๆ ไม่ทำอะไร (กันไม่ให้
เดาผิดแล้วไปย้าย/ปิดตั๋วทั้งที่ข้อมูลไม่ครบ) → ถ้าเจอ:

- **status = COMPLETED** → assign ให้คนรันตาม token (**เฉพาะถ้ายังไม่มี assignee เดิม**) → post comment สรุป
  (marker `Auto Sale Note Check`, skip การโพสต์ถ้า comment ล่าสุดมี marker + status เดียวกันอยู่แล้ว) → ถ้าตั๋วยัง
  **Open** ย้ายไป **In Progress** ก่อน แล้วปิดต่อ (**Close**) ในรอบเดียวกัน พร้อม `resolution = "Won't Do"` และ
  `fixVersions = ["Won't Fix Release"]` (ตรงกับที่ทีมปิดตั๋วพวกนี้ด้วยมืออยู่แล้ว) — เอา flag ออกด้วยถ้าติดอยู่จากรอบก่อน
- **status = NEW** → assign + post comment เหมือนกัน → ถ้าตั๋วยัง **Open** ย้ายไป **In Progress** → ติด flag
  **`Impediment`** ไว้ (idempotent, เช็คซ้ำทุกรอบไม่ผูกกับจังหวะย้ายครั้งแรกเท่านั้น — เผื่อรอบก่อนติด flag ไม่สำเร็จ)

ทั้งหมดนี้เป็น movement pattern เดียวกับ `rsp_sync_check.py` เป๊ะ (Open → In Progress ก่อนเสมอ, reconcile flag/Close
จาก state ปัจจุบันทุกรอบ ไม่ใช่แค่ตอน transition edge) แค่เปลี่ยนตัวตัดสินใจจาก "ร้านไหน sync ราคาแล้วบ้าง" (GCP log)
เป็น "sale note นี้ status อะไร" (MongoDB) เท่านั้น

Transition ID ไม่ได้ hardcode ไว้ — `get_transition_id` เรียก `/issue/{key}/transitions` สดทุกครั้งแล้วหาด้วย
**ชื่อ** transition ("In Progress" / "Close") ถ้าหาไม่เจอ (เช่น workflow ถูกแก้ไปแล้ว) จะ print WARNING แล้วข้าม
ไม่ทำให้ script ทั้งรอบ crash — เหมือน `rsp_sync_check.py` เรื่อง flag (`set_flag`) ก็ใช้ endpoint เดียวกัน
(`POST /rest/greenhopper/1.0/xboard/issue/flag/flag.json`) เพราะเป็น board/customfield เดียวกัน

ไม่มี Jira token หรือ Mongo connection string ฝังอยู่ในไฟล์นี้เลยไม่ว่ากรณีไหน ปลอดภัยที่จะ commit ขึ้น public repo
เพราะ credential จริงอยู่แค่บนเครื่องผู้ใช้เท่านั้น เรียก Jira ผ่าน `curl` (ไม่ใช่ Python `urllib`) ด้วยเหตุผลเดียวกับ
`rsp_sync_check.py` (certifi bundle ไม่มี CA ขององค์กร)
