# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T15:10:11Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `8894` (`73.7%`)
- APIs without operation mapping: `3166`
- Planned institutions: `10`
- Planned APIs: `512`
- First queue: `국토교통부`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 국토교통부 | 393 | 327 | 66 | 83.2% | 66 | 0 |
| 2 | 식품의약품안전처 | 392 | 272 | 120 | 69.4% | 100 | 20 |
| 3 | 국회 국회사무처 | 277 | 0 | 277 | 0.0% | 100 | 177 |
| 4 | 성평등가족부 | 273 | 272 | 1 | 99.6% | 1 | 0 |
| 5 | 공정거래위원회 | 250 | 216 | 34 | 86.4% | 34 | 0 |
| 6 | 한국산업인력공단 | 230 | 228 | 2 | 99.1% | 2 | 0 |
| 7 | 국립암센터 | 212 | 204 | 8 | 96.2% | 8 | 0 |
| 8 | 법제처 | 203 | 5 | 198 | 2.5% | 100 | 98 |
| 9 | 경기도 광명시 | 197 | 0 | 197 | 0.0% | 100 | 97 |
| 10 | 해양수산부 | 173 | 172 | 1 | 99.4% | 1 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 국토교통부 | 15056802 | 국토교통부_국토계획구역 | 국토관리 | JSON+XML | 자동승인 | 자동승인 | 2025-07-01 |
| 국토교통부 | 15056790 | 국토교통부_표준권역 | 국토관리 | JSON+XML | 자동승인 | 자동승인 | 2025-07-01 |
| 국토교통부 | 15056674 | 국토교통부_교통링크 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-01 |
| 국토교통부 | 15056608 | 국토교통부_가축사육제한구역 | 국토관리 | JSON+XML | 자동승인 | 자동승인 | 2025-07-01 |
| 국토교통부 | 15056574 | 국토교통부_국민임대주택 | 국토관리 | JSON+XML | 자동승인 | 자동승인 | 2025-07-01 |
| 식품의약품안전처 | 15064859 | 식품의약품안전처_식품접객업정보 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2026-03-26 |
| 식품의약품안전처 | 15091535 | 식품의약품안전처_수입식품업 폐업정보 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2025-09-19 |
| 식품의약품안전처 | 15111830 | 식품의약품안전처_지하수 수질측정망 측정결과 | 식품건강 | XML | 자동승인 | 심의승인 | 2025-08-27 |
| 식품의약품안전처 | 15111829 | 식품의약품안전처_토양지하수 토양실태조사정보 | 식품건강 | XML | 자동승인 | 심의승인 | 2025-08-27 |
| 식품의약품안전처 | 15111816 | 식품의약품안전처_어류질병정보 | 식품건강 | XML | 자동승인 | 심의승인 | 2025-08-27 |
| 국회 국회사무처 | 15152558 | 국회 국회사무처_법률안 제안이유 및 주요내용 | 공공행정 | XML | 자동승인 | 심의승인 | 2025-11-17 |
| 국회 국회사무처 | 15142964 | 국회 국회사무처_회의록 대별 위원회 목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2025-04-10 |
| 국회 국회사무처 | 15126161 | 국회 국회사무처_회의별 의안목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 국회 국회사무처 | 15126160 | 국회 국회사무처_회의별 안건목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |
| 국회 국회사무처 | 15126159 | 국회 국회사무처_시정조치 결과보고서 목록 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-07-12 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
