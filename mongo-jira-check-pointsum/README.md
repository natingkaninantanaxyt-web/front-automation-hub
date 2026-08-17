# mongo-jira-check-pointsum

เช็คตั๋ว Jira "Point Sum : `<memberId>`-`<date>`" (project `SUP`, label `PS_Front`) ที่ยังไม่ปิด — รัน aggregate
เดิมที่ใช้เช็คมือ (`membership.points`: point ของ 10 record ล่าสุดที่ไม่ EXPIRED เทียบกับ totalPoint ของ ACTIVE)
ถ้าตรงกันแล้ว (`isEqual = true`) ปิดตั๋วอัตโนมัติ — เป็นหนึ่งโมดูลใน [Front Automation Hub](../README.md)
คู่กับ [gcp-jira-check-rsp](../gcp-jira-check-rsp/) (สคริปต์เดี่ยว รันจากเครื่อง ไม่มี server เหมือนกัน แต่ต่อ MongoDB
แทน GCP Cloud Logging)

## ทำไมต้องรันจากเครื่อง ไม่ใช่ในเบราว์เซอร์

ต่อ MongoDB ตรงจาก client-side JS ไม่ได้ (ไม่ใช่ HTTP, ต่อให้ทำได้ก็ไม่ควรฝัง connection string ไว้ในโค้ดที่ทุกคน
เปิด view source เห็นได้) และเรียก Jira REST API ตรงจาก browser ก็ไม่ได้เหมือนกัน (CORS + ต้องมี credential) —
งานจริงต้องรันจาก `mongo_jira_check_pointsum.py` บนเครื่องผู้ใช้เท่านั้น

## Setup

1. `pip3 install pymongo` — ถ้าเจอ error `externally-managed-environment` (พบบน Mac ที่ลง Python ผ่าน Homebrew)
   ให้ใช้ `pip3 install --user --break-system-packages pymongo` แทน
2. Jira credential — เหมือนกับ `gcp_jira_check_rsp.py` เป๊ะ (ใช้ config เดียวกันได้ถ้าตั้งไว้แล้ว): env vars
   `JIRA_URL`/`JIRA_PERSONAL_TOKEN`, หรือ `~/.mongo_jira_check.json` / `~/.rsp_sync_check.json`
3. MongoDB connection string — ไปเอา connection string PROD (Local) แบบ read-only (`support_read_only`) จาก
   Confluence: **"Tooling Onboarding Checklist"** (space TOOK) → "Setup MongoDB Connection to PROD and NEST
   BETA" → แทน `{UserName}` ใน `appName` ด้วยชื่อตัวเอง แล้วเก็บไว้ที่ env var `MONGO_URI` หรือ field `"mongo_uri"`
   ใน `~/.mongo_jira_check.json` — **ห้าม commit connection string นี้ที่ไหนเด็ดขาด** (มี username/password ฝังอยู่)

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
python3 mongo_jira_check_pointsum.py --dry-run   # ดูก่อนว่าจะทำอะไร ไม่แก้ตั๋วจริง
python3 mongo_jira_check_pointsum.py             # รันจริง
```

เพิ่ม `--skip-update-check` ถ้าไม่อยากให้เช็คเวอร์ชันตอนเทส

## Logic

`search_open_point_sum_tickets` (JQL: label `PS_Front` + summary มีคำว่า "Point Sum" + ยังไม่ปิด) →
`parse_ticket` (ดึง `doc_no` = memberId จาก wiki-table ใน description ด้วย regex) → `query_member_point`
(aggregate เดิมจาก Confluence runbook "NEST : Recon - Member Point Sum vs Point Statement" ที่ทีมใช้เช็คมือ
อยู่แล้ว) → ถ้า `isEqual = true`: assign ให้คนรันตาม token (**เฉพาะถ้ายังไม่มี assignee เดิม**) → post comment
สรุป (marker `Auto Point Sum Check`) → transition ตรงไป **Close** พร้อม `resolution = "Won't Do"` และ
`fixVersions = ["Won't Fix Release"]` (ตรงกับที่ทีมปิดตั๋วพวกนี้ด้วยมืออยู่แล้ว เช่น SUP-13422) ถ้า `isEqual = false`
ปล่อยตั๋วไว้เฉยๆ ไม่ทำอะไร (ตามคำแนะนำใน Confluence runbook — diff ระหว่างวันมักหายไปเองพอ BigQuery sync ตอนกลางคืน)

ต่างจาก `gcp_jira_check_rsp.py` ตรงที่ workflow ของตั๋วประเภทนี้ไม่มีขั้น "In Progress"/flag ระหว่างทาง — จากตั๋วเก่าที่
ปิดไปแล้ว (SUP-13422 ฯลฯ) status เดินจาก **Open ตรงไป Close** เลยในตาที่ข้อมูล sync ตรงกันแล้ว

Transition ID ไม่ได้ hardcode ไว้ — `get_transition_id` เรียก `/issue/{key}/transitions` สดทุกครั้งแล้วหาด้วย
**ชื่อ** transition ("Close") ถ้าหาไม่เจอ (เช่น workflow ถูกแก้ไปแล้ว) จะ print WARNING แล้วข้าม ไม่ทำให้ script
ทั้งรอบ crash — เหมือน `gcp_jira_check_rsp.py`

ไม่มี Jira token หรือ Mongo connection string ฝังอยู่ในไฟล์นี้เลยไม่ว่ากรณีไหน ปลอดภัยที่จะ commit ขึ้น public repo
เพราะ credential จริงอยู่แค่บนเครื่องผู้ใช้เท่านั้น เรียก Jira ผ่าน `curl` (ไม่ใช่ Python `urllib`) ด้วยเหตุผลเดียวกับ
`gcp_jira_check_rsp.py` (certifi bundle ไม่มี CA ขององค์กร)
