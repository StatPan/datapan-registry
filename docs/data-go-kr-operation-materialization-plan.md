# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-04T05:55:34Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `11569` (`95.9%`)
- APIs without operation mapping: `491`
- Planned institutions: `10`
- Planned APIs: `147`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 인천광역시 | 16 | 10 | 6 | 62.5% | 6 | 0 |
| 4 | 한국수목원정원관리원 | 16 | 10 | 6 | 62.5% | 6 | 0 |
| 5 | 한국도로교통공단 | 16 | 12 | 4 | 75.0% | 4 | 0 |
| 6 | 국민권익위원회 | 16 | 14 | 2 | 87.5% | 2 | 0 |
| 7 | 한국문화정보원 | 15 | 6 | 9 | 40.0% | 9 | 0 |
| 8 | 경상북도 안동시 | 15 | 10 | 5 | 66.7% | 5 | 0 |
| 9 | 서울특별시 서대문구 | 14 | 0 | 14 | 0.0% | 14 | 0 |
| 10 | 인천관광공사 | 14 | 6 | 8 | 42.9% | 8 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 인천광역시 | 15058906 | 인천광역시_문화기반시설 | 문화관광 | XML | 자동승인 | 자동승인 | 2026-01-05 |
| 인천광역시 | 15058085 | 인천광역시_문화재정보 | 문화관광 | XML | 자동승인 | 자동승인 | 2026-01-05 |
| 인천광역시 | 15057514 | 인천광역시_축제정보 | 문화관광 | XML | 자동승인 | 자동승인 | 2026-01-05 |
| 인천광역시 | 15057287 | 인천광역시_문화예술행사 | 문화관광 | XML | 자동승인 | 자동승인 | 2026-01-05 |
| 인천광역시 | 15013679 | 인천광역시 관광사업체 정보 | 문화관광 | XML | 자동승인 | 자동승인 | 2025-11-29 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
