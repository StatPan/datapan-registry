# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-04T03:33:10Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `11388` (`94.4%`)
- APIs without operation mapping: `672`
- Planned institutions: `10`
- Planned APIs: `216`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 농림축산식품부 농림축산검역본부 | 25 | 8 | 17 | 32.0% | 17 | 0 |
| 4 | 서울교통공사 | 25 | 11 | 14 | 44.0% | 14 | 0 |
| 5 | 기후에너지환경부 한강홍수통제소 | 25 | 12 | 13 | 48.0% | 13 | 0 |
| 6 | 한국농수산식품유통공사 | 23 | 20 | 3 | 87.0% | 3 | 0 |
| 7 | 국가유산청 국립고궁박물관 | 22 | 0 | 22 | 0.0% | 22 | 0 |
| 8 | 주택도시보증공사 | 22 | 0 | 22 | 0.0% | 22 | 0 |
| 9 | 예술경영지원센터 | 21 | 0 | 21 | 0.0% | 21 | 0 |
| 10 | 국가데이터처 | 21 | 10 | 11 | 47.6% | 11 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 농림축산식품부 농림축산검역본부 | 15102756 | 농림축산식품부 농림축산검역본부_축산차량 방문정보 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-06-08 |
| 농림축산식품부 농림축산검역본부 | 3055523 | 농림축산식품부 농림축산검역본부_가축질병발생정보 | 농축수산 | JSON+XML | 자동승인 | 자동승인 | 2025-09-18 |
| 농림축산식품부 농림축산검역본부 | 15118023 | 농림축산식품부 농림축산검역본부_수입축산물이력정보 | 농축수산 | JSON+XML | 자동승인 | 심의승인 | 2025-09-18 |
| 농림축산식품부 농림축산검역본부 | 15103013 | 농림축산식품부 농림축산검역본부_농림축산식품부_농림축산검역본부 질병발생 통계 | 농축수산 | JSON+XML | 자동승인 | 심의승인 | 2025-09-18 |
| 농림축산식품부 농림축산검역본부 | 15102911 | 농림축산식품부 농림축산검역본부_수출식물검역정보 | 농축수산 | JSON+XML | 자동승인 | 심의승인 | 2025-09-18 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
