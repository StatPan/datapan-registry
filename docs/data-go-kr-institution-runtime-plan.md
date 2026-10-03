# data.go.kr Institution Runtime Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns the highest-priority institution runtime gaps into bounded `datapan catalog verify --org` batches.

- Generated at: `2026-07-04T06:39:24Z`
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
| 1 | 행정안전부 | 1252 | 1252 | 0 | 1767 | 1252 | 1767 | 100 |
| 2 | 경기도 | 840 | 840 | 0 | 2241 | 840 | 2241 | 100 |
| 3 | 국토교통부 | 393 | 393 | 0 | 1055 | 393 | 1055 | 100 |
| 4 | 식품의약품안전처 | 392 | 392 | 0 | 648 | 392 | 648 | 100 |
| 5 | 국회 국회사무처 | 277 | 277 | 0 | 277 | 277 | 277 | 100 |
| 6 | 성평등가족부 | 273 | 273 | 0 | 348 | 273 | 348 | 100 |
| 7 | 부산광역시 | 259 | 259 | 0 | 336 | 259 | 336 | 100 |
| 8 | 공정거래위원회 | 250 | 250 | 0 | 353 | 250 | 353 | 100 |
| 9 | 한국산업인력공단 | 230 | 230 | 0 | 317 | 230 | 317 | 100 |
| 10 | 한국마사회 | 223 | 223 | 0 | 223 | 223 | 223 | 100 |

## Batch Outputs

| Rank | Institution | Output |
| --- | ---: | ---: |
| 1 | 행정안전부 | `reports/data-go-kr/institution-batches/institution-01.json` |
| 2 | 경기도 | `reports/data-go-kr/institution-batches/institution-02.json` |
| 3 | 국토교통부 | `reports/data-go-kr/institution-batches/institution-03.json` |
| 4 | 식품의약품안전처 | `reports/data-go-kr/institution-batches/institution-04.json` |
| 5 | 국회 국회사무처 | `reports/data-go-kr/institution-batches/institution-05.json` |
| 6 | 성평등가족부 | `reports/data-go-kr/institution-batches/institution-06.json` |
| 7 | 부산광역시 | `reports/data-go-kr/institution-batches/institution-07.json` |
| 8 | 공정거래위원회 | `reports/data-go-kr/institution-batches/institution-08.json` |
| 9 | 한국산업인력공단 | `reports/data-go-kr/institution-batches/institution-09.json` |
| 10 | 한국마사회 | `reports/data-go-kr/institution-batches/institution-10.json` |

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
