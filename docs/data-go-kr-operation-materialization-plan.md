# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-04T06:24:47Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `11581` (`96.0%`)
- APIs without operation mapping: `479`
- Planned institutions: `10`
- Planned APIs: `144`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 한국도로교통공단 | 16 | 12 | 4 | 75.0% | 4 | 0 |
| 4 | 국민권익위원회 | 16 | 14 | 2 | 87.5% | 2 | 0 |
| 5 | 한국문화정보원 | 15 | 6 | 9 | 40.0% | 9 | 0 |
| 6 | 경상북도 안동시 | 15 | 10 | 5 | 66.7% | 5 | 0 |
| 7 | 서울특별시 서대문구 | 14 | 0 | 14 | 0.0% | 14 | 0 |
| 8 | 인천관광공사 | 14 | 6 | 8 | 42.9% | 8 | 0 |
| 9 | 국가보훈부 | 14 | 8 | 6 | 57.1% | 6 | 0 |
| 10 | 광주교통공사 | 14 | 11 | 3 | 78.6% | 3 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 한국도로교통공단 | 15113414 | 한국도로교통공단_화물차 교통사고 다발지역 | 공공행정 | JSON+XML | 자동승인 | 심의승인 | 2025-08-01 |
| 한국도로교통공단 | 15113413 | 한국도로교통공단_음주운전 교통사고 다발지역 | 공공행정 | JSON+XML | 자동승인 | 심의승인 | 2025-08-01 |
| 한국도로교통공단 | 15105289 | 한국도로교통공단_보행자 교통사고 다발지역 | 공공행정 | JSON+XML | 자동승인 | 심의승인 | 2025-08-01 |
| 한국도로교통공단 | 15105286 | 한국도로교통공단_이륜차 교통사고 다발지역 | 공공행정 | JSON+XML | 자동승인 | 심의승인 | 2025-08-01 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
