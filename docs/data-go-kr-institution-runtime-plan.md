# data.go.kr Institution Runtime Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns the highest-priority institution runtime gaps into bounded `datapan catalog verify --org` batches.

- Generated at: `2026-07-02T22:51:13Z`
- Planned institutions: `10`
- Planned operations: `924`
- First queue: `행정안전부`
- Batch size: `100`
- Timeout: `20s`
- Credential required: `true`

data.go.kr gateway verification requires a service key; no-key runs only prove parameter readiness.

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | Ops | Runtime Reactivation APIs | Missing Evidence Ops | Planned Ops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 행정안전부 | 1252 | 1084 | 168 | 1375 | 1060 | 1345 | 100 |
| 2 | 경기도 | 840 | 199 | 641 | 513 | 185 | 484 | 100 |
| 3 | 국토교통부 | 393 | 127 | 266 | 397 | 124 | 372 | 100 |
| 4 | 식품의약품안전처 | 392 | 272 | 120 | 363 | 270 | 361 | 100 |
| 5 | 성평등가족부 | 273 | 272 | 1 | 347 | 271 | 346 | 100 |
| 6 | 공정거래위원회 | 250 | 216 | 34 | 295 | 216 | 295 | 100 |
| 7 | 한국마사회 | 223 | 223 | 0 | 223 | 214 | 214 | 100 |
| 8 | 국립암센터 | 212 | 204 | 8 | 367 | 198 | 288 | 100 |
| 9 | 부산광역시 | 259 | 259 | 0 | 336 | 201 | 276 | 100 |
| 10 | 제주특별자치도 | 171 | 18 | 153 | 28 | 16 | 24 | 24 |

## Batch Outputs

| Rank | Institution | Output |
| --- | ---: | ---: |
| 1 | 행정안전부 | `reports/data-go-kr/institution-batches/institution-01.json` |
| 2 | 경기도 | `reports/data-go-kr/institution-batches/institution-02.json` |
| 3 | 국토교통부 | `reports/data-go-kr/institution-batches/institution-03.json` |
| 4 | 식품의약품안전처 | `reports/data-go-kr/institution-batches/institution-04.json` |
| 5 | 성평등가족부 | `reports/data-go-kr/institution-batches/institution-05.json` |
| 6 | 공정거래위원회 | `reports/data-go-kr/institution-batches/institution-06.json` |
| 7 | 한국마사회 | `reports/data-go-kr/institution-batches/institution-07.json` |
| 8 | 국립암센터 | `reports/data-go-kr/institution-batches/institution-08.json` |
| 9 | 부산광역시 | `reports/data-go-kr/institution-batches/institution-09.json` |
| 10 | 제주특별자치도 | `reports/data-go-kr/institution-batches/institution-10.json` |

## First Commands

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --org '행정안전부' --kind data_go_kr_gateway --exclude-input reports/latest-verification.json --limit 100 --timeout 20s --output reports/data-go-kr/institution-batches/institution-01.json --json
```
```bash
datapan catalog verify --registry data/data-go-kr.registry.json --org '경기도' --kind data_go_kr_gateway --exclude-input reports/latest-verification.json --limit 100 --timeout 20s --output reports/data-go-kr/institution-batches/institution-02.json --json
```
```bash
datapan catalog verify --registry data/data-go-kr.registry.json --org '국토교통부' --kind data_go_kr_gateway --exclude-input reports/latest-verification.json --limit 100 --timeout 20s --output reports/data-go-kr/institution-batches/institution-03.json --json
```

After a completed batch, merge it into `reports/latest-verification.json`, regenerate the verification summary, coverage backlog, institution overview, and this plan.
