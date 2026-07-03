# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T17:22:34Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `9700` (`80.4%`)
- APIs without operation mapping: `2360`
- Planned institutions: `10`
- Planned APIs: `656`
- First queue: `경기도 광명시`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 경기도 광명시 | 197 | 100 | 97 | 50.8% | 97 | 0 |
| 2 | 해양수산부 | 173 | 172 | 1 | 99.4% | 1 | 0 |
| 3 | 제주특별자치도 | 171 | 18 | 153 | 10.5% | 100 | 53 |
| 4 | 농촌진흥청 | 136 | 14 | 122 | 10.3% | 100 | 22 |
| 5 | 대전광역시 서구 | 125 | 0 | 125 | 0.0% | 100 | 25 |
| 6 | 전라남도 | 109 | 108 | 1 | 99.1% | 1 | 0 |
| 7 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 8 | 충청남도 | 96 | 69 | 27 | 71.9% | 27 | 0 |
| 9 | 한국도로공사 | 93 | 0 | 93 | 0.0% | 93 | 0 |
| 10 | 기상청 | 89 | 44 | 45 | 49.4% | 45 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 경기도 광명시 | 15042319 | 경기도_광명시_가로등 현황 | 재난안전 | JSON+XML | 자동승인 | 자동승인 | 2019-12-30 |
| 경기도 광명시 | 15040263 | 경기도_광명시_배수펌프장 가동현황 | 재난안전 | JSON+XML | 자동승인 | 자동승인 | 2019-10-02 |
| 경기도 광명시 | 15033117 | 경기도 광명시_상수도 보급  집계 현황 | 국토관리 | JSON+XML | 자동승인 | 자동승인 | 2018-11-08 |
| 경기도 광명시 | 15033116 | 경기도 광명시_사용승인허가 현황 | 국토관리 | JSON+XML | 자동승인 | 자동승인 | 2018-11-08 |
| 경기도 광명시 | 15033095 | 경기도 광명시_가로등 집계현황 | 국토관리 | JSON+XML | 자동승인 | 자동승인 | 2018-11-08 |
| 해양수산부 | 15084033 | 해양수산부_선박위치정보(연안AIS) 통계정보 | 농축수산 | JSON | 자동승인 | 심의승인 | 2025-08-05 |
| 제주특별자치도 | 15079829 | 제주특별자치도_제주 C-ITS 교통정보 | 교통물류 | XML | 자동승인 | 심의승인 | 2026-04-06 |
| 제주특별자치도 | 15152721 | 제주특별자치도_제주항일기념관 소장유물 | 문화관광 | JSON | 자동승인 | 심의승인 | 2025-11-18 |
| 제주특별자치도 | 15152720 | 제주특별자치도_제주공익활동지원센터 행사홍보 | 문화관광 | JSON | 자동승인 | 심의승인 | 2025-11-18 |
| 제주특별자치도 | 15152716 | 제주특별자치도_상하수도본부 단수공사안내 | 환경기상 | JSON | 자동승인 | 심의승인 | 2025-11-18 |
| 제주특별자치도 | 15152712 | 제주특별자치도_공공도서관 프로그램 현황 | 교육 | JSON | 자동승인 | 심의승인 | 2025-11-18 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
