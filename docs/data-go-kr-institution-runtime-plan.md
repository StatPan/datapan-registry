# data.go.kr Institution Runtime Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns the highest-priority institution runtime gaps into bounded `datapan catalog verify --org` batches.

- Generated at: `2026-07-03T17:58:13Z`
- Planned institutions: `10`
- Planned operations: `1000`
- First queue: `행정안전부`
- Batch size: `100`
- Timeout: `20s`
- Credential required: `true`

data.go.kr gateway verification requires a service key; no-key runs only prove parameter readiness.

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | Ops | Runtime Reactivation APIs | Missing Evidence Ops | Planned Ops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 행정안전부 | 1252 | 1252 | 0 | 1767 | 1202 | 1671 | 100 |
| 2 | 경기도 | 840 | 840 | 0 | 2241 | 754 | 2080 | 100 |
| 3 | 식품의약품안전처 | 392 | 392 | 0 | 648 | 371 | 607 | 100 |
| 4 | 국토교통부 | 393 | 393 | 0 | 1055 | 358 | 968 | 100 |
| 5 | 성평등가족부 | 273 | 273 | 0 | 348 | 271 | 346 | 100 |
| 6 | 국회 국회사무처 | 277 | 277 | 0 | 277 | 247 | 247 | 100 |
| 7 | 공정거래위원회 | 250 | 250 | 0 | 353 | 233 | 330 | 100 |
| 8 | 한국마사회 | 223 | 223 | 0 | 223 | 213 | 213 | 100 |
| 9 | 부산광역시 | 259 | 259 | 0 | 336 | 201 | 276 | 100 |
| 10 | 국립암센터 | 212 | 212 | 0 | 375 | 198 | 288 | 100 |

## Batch Outputs

| Rank | Institution | Output |
| --- | ---: | ---: |
| 1 | 행정안전부 | `reports/data-go-kr/institution-batches/institution-01.json` |
| 2 | 경기도 | `reports/data-go-kr/institution-batches/institution-02.json` |
| 3 | 식품의약품안전처 | `reports/data-go-kr/institution-batches/institution-03.json` |
| 4 | 국토교통부 | `reports/data-go-kr/institution-batches/institution-04.json` |
| 5 | 성평등가족부 | `reports/data-go-kr/institution-batches/institution-05.json` |
| 6 | 국회 국회사무처 | `reports/data-go-kr/institution-batches/institution-06.json` |
| 7 | 공정거래위원회 | `reports/data-go-kr/institution-batches/institution-07.json` |
| 8 | 한국마사회 | `reports/data-go-kr/institution-batches/institution-08.json` |
| 9 | 부산광역시 | `reports/data-go-kr/institution-batches/institution-09.json` |
| 10 | 국립암센터 | `reports/data-go-kr/institution-batches/institution-10.json` |

## First Commands

```bash
datapan catalog verify --registry data/data-go-kr.registry.json --org '행정안전부' --kind data_go_kr_gateway --exclude-input reports/latest-verification.json --limit 100 --timeout 20s --output reports/data-go-kr/institution-batches/institution-01.json --json
```
```bash
datapan catalog verify --registry data/data-go-kr.registry.json --org '경기도' --kind data_go_kr_gateway --exclude-input reports/latest-verification.json --limit 100 --timeout 20s --output reports/data-go-kr/institution-batches/institution-02.json --json
```
```bash
datapan catalog verify --registry data/data-go-kr.registry.json --org '식품의약품안전처' --kind data_go_kr_gateway --exclude-input reports/latest-verification.json --limit 100 --timeout 20s --output reports/data-go-kr/institution-batches/institution-03.json --json
```

After a completed batch, merge it into `reports/latest-verification.json`, regenerate the verification summary, coverage backlog, institution overview, and this plan.
