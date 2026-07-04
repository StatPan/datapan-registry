# Registry Standardization Blueprint

`datapan-registry` should become the public data standardization ledger for
Datapan. The repository should make source contracts, coverage, evidence,
errors, and downstream impact explicit enough that data work can be reviewed,
released, and monitored without relying on tribal knowledge.

This blueprint is the planning layer above the individual schemas and reports.
Every registry PR should be able to explain which gap in this document it
reduces.

Use `docs/registry-governance-policy.md` as the policy layer for gap statements,
quality gates, naming, warning handling, and client/server integration
boundaries. A milestone is not complete if it reaches the artifact checklist
while violating that policy.

## North Star

For every supported public data source, Datapan should be able to answer:

- Where is the official source documentation and when was it reviewed?
- Which APIs, datasets, operations, and endpoints are part of the published
  registry?
- Which operations are callable, blocked, deprecated, approval-required, or
  unsupported?
- Which source-specific behaviors affect auth, pagination, response parsing,
  rate limits, and error handling?
- Which runtime checks prove the source still behaves as expected?
- Which downstream systems need action when the registry changes?

The registry is successful when public data changes become measurable release
events instead of ad hoc debugging sessions.

## Target Architecture

The target registry has five connected layers:

1. Source inventory
   - source identity, official references, review timestamps, source family.
   - target artifact: `sources/<source_id>.json`.
   - schema: `datapan.source-profile.v1`.

2. Normalized registry artifacts
   - source-specific registry files under `data/`.
   - compatibility path for `data/data-go-kr.registry.json`.
   - release-wide `manifest.json`.

3. Evidence reports
   - coverage, audit, dependencies, route disposition, verification, and
     readiness.
   - source-scoped reports under `reports/<source>/` plus release-wide rollups.

4. Error and action routing
   - error field inventory from the registry.
   - source-specific error action catalog rules.
   - impact plan action hints for downstream repositories.

5. Gates and automation
   - release verification and readiness.
   - scheduled health checks.
   - source reference drift checks.
   - provider/runtime verification matrix.
   - downstream impact plan generation.

## Current State

Current strengths:

- `manifest.json` provides artifact checksums for the current release.
- `schemas/` contains versioned JSON contracts for existing release reports.
- `sources/data_go_kr.json` validates against `datapan.source-profile.v1`.
- `sources/kosis.json`, `sources/ecos.json`, `sources/open_assembly.json`,
  and `sources/seoul_open_data.json` validate against
  `datapan.source-profile.v1`.
- `reports/data-go-kr/error-action-catalog.json` validates against
  `datapan.error-action-catalog.v1`.
- `reports/kosis/error-action-catalog.json`,
  `reports/ecos/error-action-catalog.json`,
  `reports/open-assembly/error-action-catalog.json`, and
  `reports/seoul-open-data/error-action-catalog.json` provide draft
  source-specific action routing for the first non-data.go.kr profile batch.
- `reports/data-go-kr/external-coverage-summary.json` separates raw external
  adapter coverage from evidence-adjusted adapter candidates.
- `reports/data-go-kr/external-adapter-backlog.json` turns current
  route-disposition adapter candidates into a host/API implementation queue
  while excluding dead-route and transient-failure evidence from adapter work.
- `reports/data-go-kr/registry-impact-plan.json` validates downstream action
  hints for the current data.go.kr registry-only changes.
- `reports/registry-impact-plan.json` is generated from checked-in
  source-scoped impact plans and validates as the release-wide client/server
  action rollup.
- `reports/source-reference-drift.json` validates a manual baseline for
  official source references from every checked-in source profile.
- `reports/data-go-kr/runtime-evidence-growth.json` measures current runtime
  evidence against the 10% target and validates the next planned verification
  batches.
- `reports/data-go-kr/coverage-backlog.json` tracks `772` data.go.kr APIs
  without operation mappings, `9,285` operation-mapped APIs without runtime
  evidence, and `204` APIs with failed runtime evidence that need repair.
- `reports/kosis/runtime-evidence-plan.json`,
  `reports/ecos/runtime-evidence-plan.json`,
  `reports/open-assembly/runtime-evidence-plan.json`, and
  `reports/seoul-open-data/runtime-evidence-plan.json` record the first
  non-data.go.kr sources as `0` runtime-evidence sources with explicit blocker
  and warning IDs instead of treating missing evidence as ready.
- `reports/kosis/runtime-candidates.json`,
  `reports/ecos/runtime-candidates.json`,
  `reports/open-assembly/runtime-candidates.json`, and
  `reports/seoul-open-data/runtime-candidates.json` pin official first-batch
  runtime candidates without claiming that runtime evidence has been collected.
- `reports/source-runtime-evidence-rollup.json` rolls those source runtime
  evidence plans into a release-wide inventory of `4` sources, `0` runtime
  checks, `12` blocking blockers, and `8` warning instances after Seoul Open
  Data error taxonomy verification in Gira #67, KOSIS error taxonomy
  verification in Gira #69, ECOS error taxonomy verification in Gira #71, and
  Open Assembly error taxonomy verification in Gira #73, then runtime
  candidate batch pinning in Gira #75.
- `docs/source-runtime-readiness.md` is generated from the release-wide source
  runtime evidence rollup and checked-in source plans, so operators can read
  the same blocker, warning, and next-action inventory that CI validates.
- `reports/source-report-inventory.json` is generated from source profiles and
  checked-in `reports/<source>/` directories, measuring `5` source report
  directories, `96` source-scoped JSON reports, and `12.3%` recommended
  source-scoped report coverage.
- `scripts/sync-release-schema-artifacts.py` checks that every checked-in
  `schemas/*.schema.json` file is represented in `schemas/index.json` and
  `manifest.json`; Gira #77 raises release schema coverage from `20` to `28`
  artifacts, and Gira #85 adds the source report inventory schema as the
  `29`th checked-in schema artifact while keeping readiness warnings at `0`.
- `reports/registry-impact-plan.json` now carries a
  `registry:schema-release-surface` impact entry, and
  `scripts/validate-impact-plans.py` fails if release readiness reports schema
  coverage beyond datapan-cli's known schema set without a downstream impact
  action. Gira #79 tracks this as a datapan-cli manual investigation while
  keeping Dataset API, SDK, MCP, and datapan-data actions at `no_action`.
- `reports/coverage.json` reports high callable-operation coverage.
- `reports/route-disposition.json` separates dead-route candidates from
  transient failures.
- `reports/error-catalog.json` inventories response error/status fields.
- GitHub Actions verifies the current release surface with Git LFS enabled.

Current gaps:

- Source identity is still mostly implicit in paths and provider names.
- `sources/data_go_kr.json` establishes the baseline source profile, and the
  first non-data.go.kr profile batch is checked in.
- data.go.kr gateway coverage and data.go.kr external endpoint coverage are
  documented, but not yet generated by `datapan-cli` as source-scoped release
  artifacts.
- Error inventory has draft action routing for data.go.kr and the first
  non-data.go.kr profile batch. Gira #63 adds source-scoped runtime evidence
  plans for those non-data.go.kr sources, and Gira #65 rolls them up so release
  operators can see the remaining blocker and warning IDs centrally. Actual
  runtime evidence remains `0` for each source until adapters, credentials,
  sample parameters, and source-scoped candidate artifacts are in place.
  Gira #67 verifies Seoul Open Data's official RESULT-code taxonomy and reduces
  `source_runtime_error_taxonomy_pending` from `4` sources to `3`.
  Gira #69 verifies KOSIS official `err`/`errMsg` taxonomy and reduces
  `source_runtime_error_taxonomy_pending` from `3` sources to `2`.
  Gira #71 verifies ECOS official `RESULT.CODE`/`RESULT.MESSAGE` taxonomy and
  reduces `source_runtime_error_taxonomy_pending` from `2` sources to `1`.
  Gira #73 verifies Open Assembly official `RESULT.CODE`/`RESULT.MESSAGE`
  taxonomy and reduces `source_runtime_error_taxonomy_pending` from `1` source
  to `0`.
  Gira #75 pins official runtime candidate batches for KOSIS, ECOS, Open
  Assembly, and Seoul Open Data, reducing
  `source_runtime_manual_samples_unpinned` from `4` sources to `0` and removing
  `sample_parameters_not_pinned`/`runtime_catalog_not_materialized` blockers
  for the candidate-batch stage. Actual runtime evidence remains `0` for each
  source until credentials and bounded runtime runs exist; Gira #111 registers
  the Seoul Open Data adapter and reduces
  `source_runtime_adapter_not_registered` from `4` sources to `3`.
- Runtime evidence coverage is much lower than callable coverage. Gira #19,
  Gira #21, Gira #23, Gira #25, Gira #27, Gira #29, Gira #31, Gira #33, and
  Gira #35 raise data.go.kr runtime evidence from `256` to `626`. Gira #39,
  Gira #41, Gira #43, Gira #45, Gira #49, Gira #51, Gira #53, Gira #55, Gira
  #57, Gira #59, and Gira #61 continue gateway boundary evidence growth to
  `1221`, and Gira #93 grows it to `1231`, meeting the then-current `10%`
  runtime evidence target and clearing the release readiness warning. Gira #97
  adds `18` verified data-gg landing-page checks, Gira #99 adds `5` verified nfqs
  landing-page checks, Gira #101 adds `4` verified nongsaro landing-page
  checks, Gira #103 adds `3` verified gwanak landing-page checks, and Gira
  #105 adds `3` verified mafra landing-page checks, Gira #107 adds `3`
  verified garak landing-page checks, and Gira #109 adds `3` verified work24
  landing-page checks, Gira #111 adds `2` verified seoul-open-data landing-page
  checks, and Gira #113 adds `4` verified culture/happysd landing-page checks,
  and Gira #115 adds `2` verified ncpms/i815 landing-page checks, bringing
  runtime evidence to `1278`. Subsequent operation materialization raised the
  current evidence target to `1297`, then culture, data-gg, and mafra external
  endpoint batches add `25` verified landing-page checks. The Safety Map
  adapter then opens the 행정안전부 safemap batch and adds `10` verified landing-page
  checks, bringing runtime evidence to `1313`. The following 행정안전부 Safety
  Data batch adds `97` operation mappings and `10` approval-gated bounded
  checks, bringing runtime evidence to `1323`. The EShare adapter then opens
  `www.eshare.go.kr` and the next 행정안전부 batch adds `96` APIs, `121`
  operations, and `10` verified landing-page checks, bringing runtime evidence
  to `1333`. The Lofin365 adapter opens the 지방재정365 batch and adds `95`
  APIs, `239` operations, and `25` failed-but-bounded checks, bringing runtime
  evidence to `1358`. The Juso adapter opens the real-time address batch and
  adds `68` APIs, `144` operations, and `10` verified landing-page checks,
  bringing runtime evidence to `1368` while keeping the `10%`
  release-readiness target restored. The remaining 행정안전부 link-detail hosts
  then add the Food Safety Korea, INS24, Jeju Data Hub, and VWorld adapters,
  extend Seoul Open Data host coverage, add `9` operations, and merge `7`
  bounded checks, bringing runtime evidence to `1375`. The first 경기도 batch
  adds Gwangmyeong and Seogu adapters, materializes `100` APIs and `291`
  operations, and merges `31` bounded checks, bringing runtime evidence to
  `1406`. The second 경기도 batch adds the DGFCA adapter, materializes another
  `100` APIs and `282` operations, and merges `21` bounded checks, bringing
  runtime evidence to `1427`. The third 경기도 batch opens WAMIS host/port
  coverage, materializes another `100` APIs and `265` operations, and merges
  `31` bounded checks, bringing runtime evidence to `1458`. The fourth
  경기도 batch adds Gimhae, Jeju Air, and Open Law verification adapters,
  materializes another `100` APIs and `266` operations, and merges `23`
  verified bounded checks, bringing runtime evidence to `1481`. The fifth
  경기도 batch adds Dongjak and Korea Expressway verification adapters,
  materializes another `100` APIs and `261` operations, and merges `25`
  verified bounded checks, bringing runtime evidence to `1506`. The sixth
  경기도 batch adds Work, Icheon, Sisul WWW, and KISTEP verification adapters,
  materializes another `100` APIs and `252` operations, and merges `24`
  bounded checks, bringing runtime evidence to `1530`. The seventh 경기도 batch
  materializes the remaining `41` 경기도 APIs and `111` operations, and merges
  `11` verified bounded checks, bringing runtime evidence to `1541` and moving
  the first materialization queue to 국토교통부. The first 국토교통부 batch adds
  Calspia, Car365, and Codil verification adapters, materializes `100` APIs and
  `260` operations, and merges `26` bounded checks, bringing runtime evidence
  to `1567`. The second 국토교통부 batch adds Car, ITS, Jeju WWW, and KOFPI
  verification adapters, materializes another `100` APIs and `221` operations,
  and merges `22` bounded checks, bringing runtime evidence to `1589`. The final
  국토교통부 batch extends ITS host coverage, adds the STCIS verification adapter,
  materializes the remaining `66` APIs and `177` operations, and merges `18`
  verified bounded checks, bringing runtime evidence to `1607` and moving the
  first materialization queue to 식품의약품안전처. The first 식품의약품안전처 batch
  materializes `100` APIs and `234` operations, and merges `23` verified
  bounded checks, bringing runtime evidence to `1630`. Most evidence
  is still skipped boundary evidence, not proof that those operations are callable.
  The final 식품의약품안전처 batch materializes the remaining `20` APIs and `51`
  operations, and merges `30` skipped bounded checks, bringing runtime evidence
  to `1660` and moving the first materialization queue to 국회 국회사무처.
  The first 국회 국회사무처 batch adds Open Assembly adapter coverage,
  materializes `100` APIs and `100` operations, and merges `10`
  failed-but-bounded HTTP 400 checks, bringing runtime evidence to `1670`.
  The second 국회 국회사무처 batch materializes another `100` APIs and `100`
  operations, and merges `10` failed-but-bounded HTTP 400 checks, bringing
  runtime evidence to `1680`.
  The final 국회 국회사무처 batch materializes the remaining `77` APIs and `77`
  operations, and merges `10` failed-but-bounded HTTP 400 checks, bringing
  runtime evidence to `1690` and moving the first materialization queue to
  성평등가족부.
  The final 성평등가족부 batch adds Sex Offender adapter coverage, materializes
  the remaining `1` API and `1` operation, and merges `1` verified
  landing-page check, bringing runtime evidence to `1691` and moving the first
  materialization queue to 공정거래위원회.
  The 공정거래위원회 batch adds Consumer, Fair Data, and Franchise FTC adapter
  coverage, materializes the remaining `34` APIs and `58` operations, and
  merges `23` verified landing-page checks, bringing runtime evidence to
  `1714` and moving the first materialization queue to 한국산업인력공단.
- The 한국산업인력공단 batch adds WorldJob adapter coverage, materializes the
  remaining `2` APIs and `2` operations, and merges `2` verified landing-page
  checks, bringing runtime evidence to `1716` and moving the first
  materialization queue to 국립암센터.
- The 국립암센터 batch adds Cancer adapter coverage, materializes the remaining
  `8` APIs and `8` operations, and merges `8` verified landing-page checks,
  bringing runtime evidence to `1724` and moving the first materialization
  queue to 법제처.
- The first 법제처 batch extends Open Law host coverage, materializes `100`
  APIs and `161` operations, and merges `25` verified landing-page checks,
  bringing runtime evidence to `1749` while leaving 법제처 as the first
  materialization queue for its remaining `98` APIs.
- The final 법제처 batch materializes the remaining `98` APIs and `204`
  operations, and merges `25` verified Open Law landing-page checks, bringing
  runtime evidence to `1774` and moving the first materialization queue to
  경기도 광명시.
- The first 경기도 광명시 batch materializes `100` APIs and `290` operations
  using existing Gwangmyeong adapter coverage, and merges `25` verified
  landing-page checks, bringing runtime evidence to `1799` while leaving
  경기도 광명시 as the first materialization queue for its remaining `97` APIs.
- The final 경기도 광명시 batch materializes the remaining `97` APIs and `288`
  operations using existing Gwangmyeong adapter coverage, and merges `25`
  verified landing-page checks, bringing runtime evidence to `1824`, completing
  경기도 광명시 materialization at `197` of `197` APIs, and moving the first
  materialization queue to 해양수산부.
- The 해양수산부 GICOMS batch adds `www.gicoms.go.kr` adapter coverage,
  materializes the remaining `1` API and `1` operation, and merges `1`
  failed-but-bounded timeout check, bringing runtime evidence to `1825`,
  completing 해양수산부 materialization at `173` of `173` APIs, and moving the
  first materialization queue to 제주특별자치도.
- The first 제주특별자치도 batch adds Gwangjin, Jeju ITS, MND Open Data, and
  Seogwipo adapter coverage, materializes `100` APIs and `221` operations, and
  merges `24` verified landing-page checks plus `1` failed bounded check,
  bringing runtime evidence to `1850` while leaving 제주특별자치도 as the first
  materialization queue for its remaining `53` APIs.
- The final 제주특별자치도 batch materializes the remaining `53` APIs and `142`
  operations, and merges `25` verified landing-page checks, bringing runtime
  evidence to `1875`, completing 제주특별자치도 materialization at `171` of `171`
  APIs, and moving the first materialization queue to 농촌진흥청.
- The first 농촌진흥청 batch extends Nongsaro host coverage to bare
  `nongsaro.go.kr`, adds PSIS verification adapter coverage, materializes `100`
  APIs and `260` operations, and merges `21` verified landing-page checks plus
  `4` skipped parameter-blocked checks. That brings runtime evidence to
  `1900`, registered external adapter operations to `6640`, and leaves
  농촌진흥청 as the first materialization queue with `22` uncovered APIs.
- The final 농촌진흥청 batch adds NABIC adapter coverage, materializes the
  remaining `22` APIs and `58` operations, and merges `25` verified
  landing-page checks. That brings runtime evidence to `1925`, registered
  external adapter operations to `6698`, completes 농촌진흥청 materialization at
  `136` of `136` APIs, and moves the first materialization queue to
  대전광역시 서구.
- The first 대전광역시 서구 batch extends HappySD host coverage to
  `parking.happysd.or.kr`, materializes `100` APIs and `139` operations, and
  merges `25` verified landing-page checks. That brings runtime evidence to
  `1950`, registered external adapter operations to `6837`, and leaves
  대전광역시 서구 as the first materialization queue with `25` uncovered APIs.
- The final 대전광역시 서구 batch materializes the remaining `25` APIs and `32`
  operations, and merges `25` verified landing-page checks. That brings runtime
  evidence to `1975`, registered external adapter operations to `6869`,
  completes 대전광역시 서구 materialization at `125` of `125` APIs, and moves
  the first materialization queue to 전라남도.
- The final 전라남도 batch adds Jeonnam Redtable adapter coverage, materializes
  the remaining `1` API and `1` operation, and merges `1` verified landing-page
  check. That brings runtime evidence to `1976`, registered external adapter
  operations to `6870`, completes 전라남도 materialization at `109` of `109`
  APIs, and moves the first materialization queue to 울산항만공사.
- The 충청남도 batch adds Chungnam host adapter coverage, materializes `27` APIs
  and `65` operations, and merges `65` failed-but-bounded checks. That brings
  runtime evidence to `2041`, registered external adapter operations to `6935`,
  and makes the current 울산항만공사 queue explicit as blocked by upstream
  data.go.kr detail/catalog errors.
- The 한국도로공사 batch materializes `93` APIs and `271` operations, and
  merges `271` verified landing-page checks. That brings runtime evidence to
  `2312`, verified checks to `903`, and registered external adapter operations
  to `7206`.
- The 기상청 batch adds KMA API Hub adapter coverage, materializes `45` APIs
  and `45` operations, and merges `45` verified landing-page checks. That
  brings runtime evidence to `2357`, verified checks to `948`, and registered
  external adapter operations to `7251`.
- The 농림축산식품부 batch adds Smart Farm Korea and MAFRA legacy adapter
  coverage, materializes `85` APIs and `151` operations, and merges `167`
  bounded checks (`153` verified, `2` failed, `12` skipped). That brings
  runtime evidence to `2524`, verified checks to `1101`, and registered
  external adapter operations to `7402`.
- The 문화체육관광부 batch adds NAA and MUCH adapter coverage, materializes `85`
  APIs and `120` operations, and merges `126` bounded checks (`111` verified,
  `9` failed, `6` skipped). That brings runtime evidence to `2650`, verified
  checks to `1212`, and registered external adapter operations to `7522`.
- The 한국수자원공사 cleanup adds GIMS adapter coverage, materializes the
  remaining `5` APIs and `5` operations, and merges `5` bounded checks (`5`
  verified, `0` failed, `0` skipped). That brings runtime evidence to `2655`,
  verified checks to `1217`, and registered external adapter operations to
  `7527`.
- The 금융감독원 batch adds OpenDART adapter coverage, materializes `83` APIs
  and `245` operations, and merges `245` bounded checks (`0` verified, `0`
  failed, `245` skipped for missing `crtfc_key`). That brings runtime evidence
  to `2900`, verified checks to `1217`, and registered external adapter
  operations to `7772`.
- The 대전광역시 batch adds Daejeon adapter coverage, materializes `28` APIs
  and `67` operations, and merges `100` bounded checks (`42` verified, `58`
  failed, `0` skipped). That brings runtime evidence to `3000`, verified
  checks to `1259`, and registered external adapter operations to `7839`.
- The 서울특별시 batch adds Seoul TData, Seoul Map, Jongno, and KOSMES adapter
  coverage, materializes `68` APIs and `136` operations, and merges `100`
  bounded checks (`93` verified, `1` failed, `6` skipped). That brings runtime
  evidence to `3100`, verified checks to `1352`, and registered external
  adapter operations to `7975`.
- The 농림수산식품교육문화정보원 batch extends SmartFarm Korea host coverage to
  `smartfarmkorea.net`, materializes `66` APIs and `84` operations, and merges
  `91` verified bounded checks. That brings runtime evidence to `3191`,
  verified checks to `1443`, and registered external adapter operations to
  `8059`.
- The 지식재산처 batch adds KIPRIS Plus and IP-NAVI host coverage, materializes
  `51` APIs and `151` operations, folds the IP-NAVI port host into registered
  adapter coverage, and merges `100` bounded checks (`76` verified, `24`
  failed, `0` skipped). That brings runtime evidence to `3291`, verified
  checks to `1519`, and registered external adapter operations to `8210`.
- The 해양수산부 국립해양조사원 batch adds KHOA and NOSC host coverage,
  materializes `18` APIs and `30` operations, and merges `30` verified
  bounded checks. That brings runtime evidence to `3321`, verified checks to
  `1549`, and registered external adapter operations to `8240`.
- The 한국환경공단 batch adds Recycling Info host coverage, materializes `1`
  API and `1` operation, and merges `1` verified bounded check. That brings
  runtime evidence to `3322`, verified checks to `1550`, and registered
  external adapter operations to `8241`.
- The 한국환경연구원 batch adds ECVAM host coverage, materializes `65` APIs and
  `65` operations, and merges `65` verified bounded checks. That brings runtime
  evidence to `3387`, verified checks to `1615`, and registered external
  adapter operations to `8306`.
- The 충청북도 batch adds Chungbuk Tour host coverage, materializes `1` API and
  `1` operation, and merges `1` verified bounded check. That brings runtime
  evidence to `3388`, verified checks to `1616`, and registered external
  adapter operations to `8307`.
- The 한국서부발전(주) batch adds IWest host coverage, materializes `6` APIs and
  `18` operations, and merges `18` verified bounded checks. That brings runtime
  evidence to `3406`, verified checks to `1634`, and registered external
  adapter operations to `8325`.
- The 기후에너지환경부 국립환경과학원 batch adds NIER NESC host coverage,
  materializes `1` API and `1` operation, and merges `1` verified bounded
  check. That brings runtime evidence to `3407`, verified checks to `1635`, and
  registered external adapter operations to `8326`.
- The 서울시설공단 batch materializes `5` APIs and `11` operations, and merges
  `31` bounded checks (`11` verified and `20` skipped boundary records). That
  brings runtime evidence to `3438`, verified checks to `1646`, and registered
  external adapter operations to `8337`.
- The 국립생태원 batch registers NIE Ecobank host coverage, materializes `39`
  APIs and `39` operations, and merges `39` verified landing-page checks. That
  brings runtime evidence to `3477`, verified checks to `1685`, and registered
  external adapter operations to `8376`.
- The 해양수산부 국립수산물품질관리원 batch materializes `37` APIs and `45`
  operations, and merges `50` verified landing-page checks. That brings runtime
  evidence to `3527`, verified checks to `1735`, and registered external
  adapter operations to `8421`.
- The 서울특별시 동작구 batch materializes `36` APIs and `95` operations, and
  merges `95` bounded checks (`70` verified and `25` skipped for Seoul Open Data
  auth). That brings runtime evidence to `3622`, verified checks to `1805`, and
  registered external adapter operations to `8516`.
- The 관세청 batch registers UniPass host coverage, materializes `2` APIs and
  `3` operations, and merges `3` verified landing-page checks. That brings
  runtime evidence to `3625`, verified checks to `1808`, and registered external
  adapter operations to `8519`.
- The 과학기술정보통신부 우정사업본부 batch registers KoreaPost host coverage,
  materializes `1` API and `1` operation, and merges `29` bounded checks (`1`
  verified and `28` existing EPost boundary skips). That brings runtime evidence
  to `3654`, verified checks to `1809`, and registered external adapter
  operations to `8520`.
- The 한국사회보장정보원 batch registers Childcare Info host coverage,
  materializes `23` APIs and `23` operations, and merges `23` verified
  landing-page checks. That brings runtime evidence to `3677`, verified checks
  to `1832`, and registered external adapter operations to `8543`.
- The 대구광역시 batch registers Daegu host coverage, materializes `13` APIs
  and `28` operations, and merges `32` bounded checks (`28` verified and `4`
  skipped for existing car.daegu.go.kr parameter gaps). That brings runtime
  evidence to `3709`, verified checks to `1860`, and registered external
  adapter operations to `8571`.
- The 한국고용정보원 batch registers YouthCenter host coverage, materializes
  `24` APIs and `68` operations, and merges `89` verified Work24/Work/
  YouthCenter landing-page checks. That brings runtime evidence to `3798`,
  verified checks to `1949`, and registered external adapter operations to
  `8639`.
- The 국가유산청 국립무형유산원 batch registers NIHC host coverage,
  materializes `15` APIs and `18` operations, and merges `18` verified
  landing-page checks. That brings runtime evidence to `3816`, verified checks
  to `1967`, and registered external adapter operations to `8657`.
- The 울산광역시 batch registers Ulsan WWW host coverage, materializes `10`
  APIs and `30` operations, and merges `30` verified bounded provider checks.
  That brings runtime evidence to `3846`, verified checks to `1997`, and
  registered external adapter operations to `8687`.
- The 농림축산식품부 국립농산물품질관리원 batch materializes `21` APIs
  and `49` operations, and merges `49` verified MAFRA landing-page checks.
  That brings runtime evidence to `3895`, verified checks to `2046`, and
  registered external adapter operations to `8736`.
- The 제주특별자치도 서귀포시 batch expands Seogwipo host coverage,
  materializes `28` APIs and `70` operations, and merges `61` verified
  Seogwipo bounded checks. That brings runtime evidence to `3956`, verified
  checks to `2107`, and registered external adapter operations to `8806`.
- The 서울특별시농수산식품공사 batch extends Garak host coverage to
  `temp.garak.co.kr`, materializes `27` APIs and `81` operations, and merges
  `81` verified Garak bounded checks. That brings runtime evidence to `4037`,
  verified checks to `2188`, and registered external adapter operations to
  `8887`.
- The 국가유산청 국립문화유산연구원 batch adds NRich host coverage for
  `portal.nrich.go.kr` and `www.nrich.go.kr`, materializes `27` APIs and
  `76` operations, and merges `76` verified NRich bounded checks. That brings
  runtime evidence to `4113`, verified checks to `2264`, and registered
  external adapter operations to `8963`.
- The 경기도 안양시 batch adds Anyang and MPVA Egonghun host coverage,
  materializes `26` APIs and `41` operations, and merges `28` bounded checks
  (`2` verified and `26` failed request-boundary records). That brings runtime
  evidence to `4141`, verified checks to `2266`, and registered external
  adapter operations to `9004`.
- The 대전교통공사 batch adds Tashu host coverage, materializes the remaining
  `1` API and `1` operation, and merges `1` verified Tashu bounded check. That
  brings runtime evidence to `4142`, verified checks to `2267`, and registered
  external adapter operations to `9005`.
- The 국가철도공단 batch adds KRIC host coverage for `data.kric.go.kr`,
  materializes `26` APIs and `78` operations, and merges `78` verified KRIC
  bounded checks. That brings runtime evidence to `4220`, verified checks to
  `2345`, and registered external adapter operations to `9083`.
- API operation coverage remains incomplete: `11,288` of `12,060` APIs have
  operation mappings (`93.6%`), leaving `772` APIs to materialize and `9,285`
  operation-mapped APIs to reactivate with runtime evidence.
- Multi-source report grouping is measured by
  `reports/source-report-inventory.json`, but full source-scoped report
  generation remains incomplete.
- Impact plans are specified, a data.go.kr draft plan is checked in, and a
  release-wide rollup can be generated from source-scoped plans, but full
  `datapan-cli` generation from catalog diffs, verification evidence, route
  disposition, and promoted dataset mappings is not implemented.
- Live drift checks for official source documentation are not implemented, but
  checked-in source reference baselines are now validated against source
  profiles.
- The registry release surface now includes every checked-in registry schema,
  and the current datapan-cli release readiness gate passes with
  `schema_set_complete` reporting `expected=20` and `actual=29`. Gira #79 keeps
  the broader CLI-side schema-generator follow-up explicit in the impact plan
  instead of relying on a remembered PR note.

## Gap Matrix

| Gap | Current artifact | Target artifact | Measurement |
| --- | --- | --- | --- |
| Source identity | `provider` strings and paths | `sources/<source_id>.json` | number of supported sources with valid source profiles |
| data.go.kr mastery | coverage reports plus route evidence | `sources/data_go_kr.json` and data.go.kr mastery gates | gateway, external registered, external dead/transient, and evidence-adjusted adapter coverage |
| Official references | documentation only | profile reference URLs with review dates | profiles with homepage/API/key/notice/terms references |
| Site behavior | adapter code and manual knowledge | source profile auth/request/response/runtime sections | profiles covering auth, paging, response, errors, runtime |
| Error routing | `reports/error-catalog.json` | `reports/<source>/error-action-catalog.json` | known error signatures mapped to action rules |
| Multi-source layout | root `data/` and `reports/` | source-scoped reports plus root rollups | source-scoped artifact count and release rollup coverage |
| Runtime confidence | `latest-verification.json` | scheduled source/provider verification matrix | evidence coverage percentage and provider pass/fail trend |
| Downstream impact | draft data.go.kr impact plan plus human review | generated `reports/registry-impact-plan.json` rollup | changes with explicit downstream action hints |
| Drift monitoring | manual source reference baseline | source reference drift reports plus scheduled checks | official reference URLs checked and classified |

## Milestones

### M1: Contract Baseline

Goal: make the target operating model explicit without migrating generated
artifacts.

Done when:

- multi-source layout is documented;
- source standardization research exists;
- `datapan.source-profile.v1` exists;
- `datapan.error-action-catalog.v1` exists;
- `datapan.registry-impact-plan.v1` exists;
- guarded release-draft workflow exists;
- current release verification stays green.

### M2: data.go.kr Mastery

Goal: master data.go.kr first as the reference implementation for source
profiles, source-scoped reports, coverage gates, error routing, and downstream
impact boundaries.

Done when:

- `sources/data_go_kr.json` exists and validates;
- source profile validation runs in CI;
- `docs/data-go-kr-mastery-plan.md` defines gateway and external endpoint
  coverage separately;
- data.go.kr missing external routes are governed by route-disposition evidence
  before becoming adapter work;
- data.go.kr source-scoped artifacts have an explicit generation contract;
- checked-in data.go.kr source-scoped reports validate in CI.

### M3: External Endpoint Coverage

Goal: turn data.go.kr external endpoint evidence and observed failures into
operational decisions before creating adapter backlog.

Done when:

- data.go.kr credential and approval failures have draft action rules;
- data.go.kr external route disposition reasons are mapped to action
  classifications;
- raw external adapter coverage and evidence-adjusted adapter candidates are
  reported separately;
- missing external routes without route-disposition evidence fail validation or
  become tracked warnings;
- error action catalog validation runs in CI;
- known credential, approval, rate limit, not-found, upstream, parser, and
  adapter cases have explicit actions;
- route-disposition and verification evidence can reference action rules;
- unknown signatures are counted instead of silently ignored.

### M4: Multi-Source Standardization

Goal: prove the source profile contract against official public data sites
outside data.go.kr without forcing data.go.kr-only assumptions onto them.

Done when:

- `sources/kosis.json` exists;
- `sources/ecos.json` exists;
- `sources/open_assembly.json` exists;
- `sources/seoul_open_data.json` exists;
- every profile validates against `datapan.source-profile.v1`;
- every profile has official reference URLs and `last_reviewed_at`;
- every profile records auth, request, response, errors, and runtime policy;
- every checked-in source-specific error action catalog validates and
  cross-checks its source profile identity;
- every checked-in source runtime evidence plan validates and records explicit
  blocker and warning IDs while evidence is absent;
- the release-wide source runtime evidence rollup validates against checked-in
  source plans;
- source-scoped reports are generated under `reports/<source>/`;
- root reports are documented as release-wide rollups;
- CI validates source-scoped report paths where present;
- the existing `data/data-go-kr.registry.json` compatibility path remains
  valid.

### M5: Client Server Impact Plans

Goal: make registry changes actionable for datapan-cli, datapan-api, SDK, and
MCP consumers.

Done when:

- data.go.kr changes can produce impact-plan entries from catalog diff,
  verification evidence, route disposition, and promoted dataset mappings;
- checked-in impact plans validate in CI before client/server consumers act on
  them;
- `reports/registry-impact-plan.json` is generated from checked-in
  source-scoped impact plans, and future CLI generation can replace that rollup
  with output derived from registry diffs, verification evidence, source
  profiles, error action catalogs, and promoted dataset mappings;
- registry-only additions can explicitly produce `no_action`;
- promoted dataset schema changes can explicitly produce
  `db_migration_review`;
- served dataset changes can explicitly target Dataset API, SDK, and MCP
  regeneration.

### M6: Drift and Evidence Growth

Goal: move from release-time confidence to ongoing registry health.

Done when:

- official reference drift reports exist;
- source profile reference changes fail CI unless the drift baseline is
  refreshed;
- scheduled health checks include source reference drift and provider runtime
  verification;
- live source reference drift checks run outside ordinary PR validation so
  external site outages are visible health failures without making every PR
  nondeterministic;
- data.go.kr runtime evidence growth is measured by a checked-in source-scoped
  report before additional verification batches are executed;
- data.go.kr runtime evidence coverage trends toward the documented `10%`
  target;
- external adapter coverage trends toward the documented `98%` target;
- warning annotations in CI are treated as work items, not background noise.

### Later: Broad Source Expansion

Goal: prove the source profile contract against diverse official sites.

Done when:
- data.go.kr mastery gates are stable;
- at least three materially different non-data.go.kr source profiles validate;
- source-scoped reports and impact plans can represent those sources without
  changing the data.go.kr compatibility surface.

## Task Backlog

Use this order unless a production failure changes priority:

1. Add and validate `sources/data_go_kr.json`. Done in PR #4.
2. Add profile validation to CI. Done in PR #4.
3. Add data.go.kr error action catalog draft. Done in PR #4.
4. Add evidence-adjusted external coverage summary for data.go.kr. Done in
   PR #4 as a checked-in draft artifact.
5. Generate data.go.kr source-scoped release artifacts. Done in PR #4 and
   tracked by Gira #5.
6. Operationalize data.go.kr external endpoint evidence. Done in PR #4 and
   tracked by Gira #6.
7. Add hand-reviewed profiles for KOSIS, ECOS, Open Assembly, and Seoul Open
   Data. Done in PR #4 and tracked by Gira #7.
8. Add and validate a data.go.kr impact plan for CLI and API consumers. Done
   in PR #4 and tracked by Gira #8.
9. Add draft `reports/<source>/error-action-catalog.json` files for M4
   sources. Tracked by Gira #9.
10. Add action catalog validation to CI. Done in PR #4 for checked-in draft
   catalogs.
11. Add source reference drift report schema and manual baseline. Tracked by
    Gira #11.
12. Add a manual or scheduled drift-check workflow. Tracked by Gira #13.
13. Add a data.go.kr runtime evidence growth summary. Tracked by Gira #15.
14. Expand runtime verification evidence by source/provider priority. Started
    by Gira #19 with `epost` and `ulsan` external endpoint batches and
    continued by Gira #21 with gateway, `geoje`, `jeonju`, and `q-net`
    batches, then by Gira #23 with `ekape`, `emuseum`, `uiryeong`, `epost`,
    and `ulsan` batches, by Gira #25 with the next gateway, `ekape`, `geoje`,
    `jeonju`, `q-net`, and `uiryeong` batches, by Gira #27 with the next
    gateway, `ekape`, `geoje`, `jeonju`, `q-net`, and `uiryeong` batches, and
    by Gira #29 with the next external `ekape`, `geoje`, `jeonju`, `q-net`, and
    `uiryeong` batches, by Gira #31 with the next `jeonju` and `q-net`
    batches, by Gira #33 with another `jeonju` and `q-net` batch, and by Gira
    #35 with the remaining planned `jeonju` and `q-net` external candidates,
    then by Gira #39, Gira #41, Gira #43, Gira #45, Gira #49, Gira #51, Gira
    #53, Gira #55, Gira #57, Gira #59, and Gira #61 with gateway batches;
    this is skipped boundary evidence growth, not proof that those operations
    are callable.
15. Add a release-wide registry impact plan rollup generated from checked-in
    source-scoped impact plans. Tracked by Gira #47; this establishes the
    client/server artifact path but does not complete full datapan-cli
    catalog-diff-based generation.
16. Add source-scoped runtime evidence plans for the first non-data.go.kr
    sources. Tracked by Gira #63; this records why KOSIS, ECOS, Open Assembly,
    and Seoul Open Data have `0` runtime checks and what must be built before
    evidence can be collected.
17. Add a release-wide source runtime evidence rollup. Tracked by Gira #65;
    this centralizes non-data.go.kr runtime evidence blockers and warnings
    without treating missing evidence as ready.
18. Verify Seoul Open Data error taxonomy from official RESULT-code references.
    Tracked by Gira #67; this reduces one `source_runtime_error_taxonomy_pending`
    warning while leaving remaining non-data runtime evidence blockers explicit.
19. Verify KOSIS error taxonomy from official `err`/`errMsg` references.
    Tracked by Gira #69; this reduces one more
    `source_runtime_error_taxonomy_pending` warning while preserving runtime
    evidence, adapter, and sample-parameter warnings.
20. Verify ECOS error taxonomy from official `RESULT.CODE`/`RESULT.MESSAGE`
    references. Tracked by Gira #71; this reduces one more
    `source_runtime_error_taxonomy_pending` warning while preserving runtime
    evidence, adapter, and sample-parameter warnings.
21. Verify Open Assembly error taxonomy from official
    `RESULT.CODE`/`RESULT.MESSAGE` references. Tracked by Gira #73; this clears
    the remaining `source_runtime_error_taxonomy_pending` warning while
    preserving runtime evidence, adapter, and sample-parameter warnings.
22. Add non-data source runtime candidate batches for KOSIS, ECOS, Open
    Assembly, and Seoul Open Data. Tracked by Gira #75; this validates pinned
    registry-only first-batch candidates, clears manual sample warnings, and
    leaves runtime evidence collection gated by adapters and credentials.
23. Bind all checked-in registry schemas into release schema artifacts. Tracked
    by Gira #77; this raises schema artifact coverage from `20` to `28`, adds a
    CI drift check, and keeps runtime warning IDs unchanged.
24. Add a registry schema release impact gate. Tracked by Gira #79; this
    requires `reports/registry-impact-plan.json` to carry
    `registry:schema-release-surface` whenever readiness reports
    `schema_set_complete.actual > expected`, and preserves `no_action`
    boundaries for Dataset API, SDK, MCP, and datapan-data.
25. Add a generated source runtime readiness overview. Tracked by Gira #83;
    this makes the non-data.go.kr runtime blockers and warnings readable in
    `docs/source-runtime-readiness.md` and keeps the document synced through
    `scripts/validate-source-runtime-evidence-rollup.py`.
26. Add a generated source report inventory. Tracked by Gira #85; this turns
    the multi-source report grouping gap into `reports/source-report-inventory.json`
    coverage metrics and validates drift in CI.
27. Register Seoul Open Data runtime and data.go.kr external coverage.
    Tracked by Gira #111; this adds the `seoul-open-data` adapter, refreshes
    data.go.kr evidence to `1272` checks, moves Seoul out of
    `source_runtime_adapter_not_registered`, and leaves the next source-runtime
    blocker on credentialed bounded runs.
28. Add a generated data.go.kr coverage backlog. Tracked by Gira #89; this
    turns uncovered APIs and runtime reactivation targets into
    `reports/data-go-kr/coverage-backlog.json` work queues that validate in CI.
29. Add a generated data.go.kr external adapter backlog. Tracked by Gira #95;
    this turned `47` route-disposition adapter-candidate operations into
    `12` host-scoped adapter implementation queues while keeping dead and
    transient routes out of adapter work. Gira #97 registers and verifies
    `data.gg.go.kr`, Gira #99 registers and verifies `www.nfqs.go.kr`, Gira
    #101 registers and verifies `www.nongsaro.go.kr`, Gira #103 registers and
    verifies `data.gwanak.go.kr`, Gira #105 registers and verifies
    `data.mafra.go.kr`, Gira #107 registers and verifies `www.garak.co.kr`,
    Gira #109 registers and verifies `www.work24.go.kr`, and Gira #111
    registers and verifies `data.seoul.go.kr`, reducing the active adapter
    backlog to `6` operations across `4` host-scoped queues. Gira #113
    registers and verifies `www.culture.go.kr` and `www.happysd.or.kr`,
    reducing the active adapter backlog to `2` operations across `2`
    host-scoped queues. Gira #115 registers and verifies `ncpms.rda.go.kr` and
    `search.i815.or.kr`, reducing the active adapter backlog to `0`.

## Measurement Rules

Each task should report at least one measurable outcome:

- profiles added or validated;
- official reference URLs covered;
- error signatures classified;
- unknown error signatures remaining;
- source-scoped reports generated;
- source runtime evidence blocker and warning IDs tracked;
- runtime verification checks added;
- evidence coverage percentage;
- adapter coverage percentage;
- downstream impact changes classified;
- CI warnings removed.

Avoid "support more sources" as a task description. Prefer "add validated
source profiles for KOSIS and ECOS" or "classify ECOS credential and not-found
errors".

Every non-trivial PR should include a short gap statement in its description:

- milestone targeted;
- gap reduced;
- artifact or gate changed;
- metric changed or expected to change;
- warnings introduced, resolved, or explicitly tracked.

## Decision Rules

- Do not add a source importer before a source profile exists.
- Do not add generated source reports before the source-specific report layout
  is clear.
- Do not convert an error into adapter work until the action catalog separates
  credential, approval, route, upstream, parser, and adapter causes.
- Do not require downstream migrations for registry-only additions.
- Do not hide failed or skipped verification evidence when it explains a real
  provider boundary.
- Do not update `manifest.json` or `schemas/index.json` by hand for generated
  release artifacts.
- Do not treat a warning as harmless merely because the workflow succeeds.

## Review Cadence

Review this blueprint when:

- a new source family is added;
- a release gate changes;
- runtime evidence targets change;
- a downstream repository starts consuming a new registry artifact;
- CI emits a new warning annotation;
- official source documentation changes in a way that affects contracts.
