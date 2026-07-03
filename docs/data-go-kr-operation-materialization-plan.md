# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T17:08:03Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `9502` (`78.8%`)
- APIs without operation mapping: `2558`
- Planned institutions: `10`
- Planned APIs: `712`
- First queue: `법제처`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 법제처 | 203 | 105 | 98 | 51.7% | 98 | 0 |
| 2 | 경기도 광명시 | 197 | 0 | 197 | 0.0% | 100 | 97 |
| 3 | 해양수산부 | 173 | 172 | 1 | 99.4% | 1 | 0 |
| 4 | 제주특별자치도 | 171 | 18 | 153 | 10.5% | 100 | 53 |
| 5 | 농촌진흥청 | 136 | 14 | 122 | 10.3% | 100 | 22 |
| 6 | 대전광역시 서구 | 125 | 0 | 125 | 0.0% | 100 | 25 |
| 7 | 전라남도 | 109 | 108 | 1 | 99.1% | 1 | 0 |
| 8 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 9 | 충청남도 | 96 | 69 | 27 | 71.9% | 27 | 0 |
| 10 | 한국도로공사 | 93 | 0 | 93 | 0.0% | 93 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 법제처 | 15058027 | 법제처_모바일 자치법규 목록 조회 | 공공행정 | XML | 자동승인 | 자동승인 | 2025-07-09 |
| 법제처 | 15058012 | 법제처_맞춤형 행정규칙 목록 조회 | 공공행정 | XML | 자동승인 | 자동승인 | 2025-07-09 |
| 법제처 | 15057909 | 법제처_모바일 자치법규 본문 조회 | 공공행정 | XML | 자동승인 | 자동승인 | 2025-07-09 |
| 법제처 | 15057908 | 법제처_모바일 행정규칙 본문 조회 | 공공행정 | XML | 자동승인 | 자동승인 | 2025-07-09 |
| 법제처 | 15057819 | 법제처_맞춤형 자치법규 조문 목록 조회 | 공공행정 | XML | 자동승인 | 자동승인 | 2025-07-09 |
| 경기도 광명시 | 15114779 | 경기도 광명시_박물관 미술관 | 문화관광 | XML | 자동승인 | 심의승인 | 2026-06-04 |
| 경기도 광명시 | 15032655 | 경기도 광명시_공용차량 운영현황 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-02-10 |
| 경기도 광명시 | 15040131 | 경기도_광명시_법정동별 반려동물 현황 | 농축수산 | JSON+XML | 자동승인 | 자동승인 | 2024-02-08 |
| 경기도 광명시 | 15034060 | 경기도 광명시_의무소독대상 현황 | 보건의료 | JSON+XML | 자동승인 | 자동승인 | 2023-12-06 |
| 경기도 광명시 | 15033101 | 경기도 광명시_건축허가 현황 | 국토관리 | JSON+XML | 자동승인 | 자동승인 | 2023-12-06 |
| 해양수산부 | 15084033 | 해양수산부_선박위치정보(연안AIS) 통계정보 | 농축수산 | JSON | 자동승인 | 심의승인 | 2025-08-05 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
