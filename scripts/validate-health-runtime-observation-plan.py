#!/usr/bin/env python3
"""Fail closed on the static Health observation-plan and manifest binding."""
import hashlib, importlib.util, json, pathlib, sys
import jsonschema
ROOT=pathlib.Path("."); PLAN=ROOT/"reports/health-runtime-observation-plan.v1.json"; SCHEMA=ROOT/"schemas/datapan.health-runtime-observation-plan.v1.schema.json"; MANIFEST=ROOT/"manifest.json"
FORBIDDEN={"credential","credential_value","secret","query","url","command","args","response","row","live"}
def load(p): return json.loads(p.read_text())
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def fail(x,m):
 if not x: raise ValueError(m)
def walk(v):
 if isinstance(v,dict):
  for k,x in v.items():
   fail(k.lower() not in FORBIDDEN or k in {"credential_requirement","cli_operation_key"},"forbidden plan field")
   walk(x)
 elif isinstance(v,list):
  for x in v: walk(x)
def main():
 try:
  plan,schema,manifest=load(PLAN),load(SCHEMA),load(MANIFEST)
  jsonschema.Draft202012Validator(schema,format_checker=jsonschema.FormatChecker()).validate(plan); walk(plan)
  spec=importlib.util.spec_from_file_location("plan_generator",ROOT/"scripts/generate-health-runtime-observation-plan.py"); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
  fail(plan==mod.build(),"generated plan drift")
  entries=[x for x in manifest["artifacts"] if x.get("path")==PLAN.as_posix()]
  fail(len(entries)==1 and entries[0].get("kind")=="verification_plan" and entries[0].get("schema")==schema["$id"],"plan manifest entry mismatch")
  fail(entries[0].get("sha256")==sha(PLAN) and entries[0].get("bytes")==PLAN.stat().st_size,"full manifest does not bind exact plan bytes")
  fail(plan["manifest_binding"]["sha256"]==mod.health_plan_manifest_binding(manifest),"manifest binding digest mismatch")
  shards=plan["shards"]; fail([x["index"] for x in shards]==list(range(8)),"shards must be canonical indexes")
  members=[x["members"][0] for x in shards]; fail(len({x["operation_id"] for x in members})==8,"membership overlaps")
  for shard,member in zip(shards,members,strict=True): fail(shard["membership_digest"]==hashlib.sha256(json.dumps([member],sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest(),"membership digest mismatch")
 except Exception as e: print(f"FAIL health runtime observation plan: {e}",file=sys.stderr); return 1
 print("ok health runtime observation plan validation (shards=8, redaction=verified)"); return 0
if __name__=="__main__": raise SystemExit(main())
