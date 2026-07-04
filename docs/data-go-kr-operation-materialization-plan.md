# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-04T04:56:31Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `11500` (`95.4%`)
- APIs without operation mapping: `560`
- Planned institutions: `10`
- Planned APIs: `178`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 국가데이터처 | 21 | 10 | 11 | 47.6% | 11 | 0 |
| 4 | 경찰청 | 19 | 9 | 10 | 47.4% | 10 | 0 |
| 5 | 기획예산처 | 18 | 3 | 15 | 16.7% | 15 | 0 |
| 6 | 국방부 | 17 | 0 | 17 | 0.0% | 17 | 0 |
| 7 | 한국은행 | 16 | 0 | 16 | 0.0% | 16 | 0 |
| 8 | 인천광역시 | 16 | 10 | 6 | 62.5% | 6 | 0 |
| 9 | 한국수목원정원관리원 | 16 | 10 | 6 | 62.5% | 6 | 0 |
| 10 | 한국도로교통공단 | 16 | 12 | 4 | 75.0% | 4 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 국가데이터처 | 15088190 | 국가데이터처_SGIS오픈플랫폼_인구통계 | 공공행정 | JSON | 자동승인 | 심의승인 | 2026-04-23 |
| 국가데이터처 | 15087213 | 국가데이터처_총조사 주요지표 | 공공행정 | JSON | 자동승인 | 심의승인 | 2026-04-23 |
| 국가데이터처 | 15072580 | 국가데이터처_지역현안소통지도 목록 | 공공행정 | JSON | 자동승인 | 심의승인 | 2026-04-23 |
| 국가데이터처 | 15072574 | 국가데이터처_전국 기술업종 정보 | 공공행정 | JSON | 자동승인 | 심의승인 | 2026-04-23 |
| 국가데이터처 | 15072572 | 국가데이터처_인구와 가구 목록 | 공공행정 | JSON | 자동승인 | 심의승인 | 2026-04-23 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
