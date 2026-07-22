#!/usr/bin/env python3
"""Generate the static, manifest-bound eight-shard Health execution plan."""
import copy, hashlib, json, pathlib, sys
from urllib.parse import urlsplit

ROOT=pathlib.Path("."); POLICY=ROOT/"policy/health-runtime-observation-selection.json"; REGISTRY=ROOT/"data/data-go-kr.registry.json"; MANIFEST=ROOT/"manifest.json"; OUTPUT=ROOT/"reports/health-runtime-observation-plan.v1.json"
def dump(v): return json.dumps(v,ensure_ascii=False,indent=2)+"\n"
def sha(b): return hashlib.sha256(b).hexdigest()
def key(fields):
 b=bytearray()
 for f in fields: x=f.encode(); b.extend(f"{len(x)}:".encode()); b.extend(x)
 return sha(bytes(b))
def manifest_binding(manifest):
 v=copy.deepcopy(manifest); matches=[a for a in v["artifacts"] if a.get("path")==OUTPUT.as_posix()]
 if len(matches)!=1 or matches[0].get("kind")!="health_runtime_observation_plan" or matches[0].get("schema")!="https://schemas.datapan.dev/datapan.health-runtime-observation-plan.v1.schema.json": raise ValueError("plan manifest entry must be unique with expected path/kind/schema")
 for f in ("bytes","sha256"):
  if f not in matches[0]: raise ValueError("plan manifest entry missing excluded field")
  matches[0].pop(f)
 return sha(json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode())
def build():
 p=json.loads(POLICY.read_text()); r=json.loads(REGISTRY.read_text()); m=json.loads(MANIFEST.read_text()); by={(d["id"],o["name"]):(d,o) for d in r for o in d.get("operations",[]) }
 members=[]
 for s in p["members"]:
  d,o=by.get((s["dataset_id"],s["operation_name"]),(None,None))
  if not d: raise ValueError("unknown selected operation")
  u=urlsplit(o.get("endpoint", ""));
  if u.hostname!="apis.data.go.kr" or not u.path or u.query or u.username or u.password: raise ValueError("selected endpoint is not an allowlisted host/path")
  names={str(x.get("name", "")).lower() for x in o.get("request_params",[])}
  if not {"servicekey","pageno","numofrows"}.issubset(names): raise ValueError("selected operation lacks safe contract")
  cli=key([d["provider"],d["id"],o["name"],"data_go_kr_gateway",u.hostname,u.path])
  members.append({"operation_id":s["operation_id"],"dataset_id":d["id"],"operation_name":o["name"],"upstream_operation_seq":str(o["source"]["raw"]["operation_seq"]),"cli_operation_key":cli,"endpoint":{"host":u.hostname,"path":u.path},"credential_requirement":{"required":True,"type":"service_key","scope":"operation"},"execution":p["execution"]})
 if len(members)!=8 or len({x["operation_id"] for x in members})!=8: raise ValueError("selection must have eight unique members")
 shards=[]
 for i,x in enumerate(members):
  digest=sha(json.dumps([x],sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()); shards.append({"index":i,"operation_count":1,"membership_digest":digest,"members":[x]})
 return {"schema_version":"datapan.health-runtime-observation-plan.v1","generated_at":p["generated_at"],"authority":"datapan-registry","registry_revision":p["registry_revision"],"source_registry":{"path":REGISTRY.as_posix(),"sha256":sha(REGISTRY.read_bytes())},"manifest_binding":{"path":MANIFEST.as_posix(),"sha256":manifest_binding(m),"excluded_fields":["bytes","sha256"]},"selection_policy":{"path":POLICY.as_posix(),"sha256":sha(POLICY.read_bytes()),"selection_id":p["selection_id"]},"summary":{"shards":8,"members":8},"shards":shards}
def main():
 try:
  expected=dump(build())
  if "--check" in sys.argv:
   if not OUTPUT.is_file() or OUTPUT.read_text()!=expected: raise ValueError("generated plan drift")
  else: OUTPUT.write_text(expected)
  print("ok health runtime observation plan (shards=8, members=8)")
 except Exception as e: print(f"FAIL health runtime observation plan: {e}",file=sys.stderr); return 1
 return 0
if __name__=="__main__": raise SystemExit(main())
