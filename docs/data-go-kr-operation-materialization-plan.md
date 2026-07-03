# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T18:30:56Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `10073` (`83.5%`)
- APIs without operation mapping: `1987`
- Planned institutions: `10`
- Planned APIs: `617`
- First queue: `대전광역시 서구`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 대전광역시 서구 | 125 | 0 | 125 | 0.0% | 100 | 25 |
| 2 | 전라남도 | 109 | 108 | 1 | 99.1% | 1 | 0 |
| 3 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 4 | 충청남도 | 96 | 69 | 27 | 71.9% | 27 | 0 |
| 5 | 한국도로공사 | 93 | 0 | 93 | 0.0% | 93 | 0 |
| 6 | 기상청 | 89 | 44 | 45 | 49.4% | 45 | 0 |
| 7 | 농림축산식품부 | 87 | 1 | 86 | 1.1% | 86 | 0 |
| 8 | 문화체육관광부 | 86 | 1 | 85 | 1.2% | 85 | 0 |
| 9 | 한국수자원공사 | 84 | 79 | 5 | 94.0% | 5 | 0 |
| 10 | 금융감독원 | 83 | 0 | 83 | 0.0% | 83 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 대전광역시 서구 | 15109957 | 대전광역시 서구_행정동별 연령대별 유동인구 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 대전광역시 서구 | 15108988 | 대전광역시 서구_행정동별 업종별 착한가격업소 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 대전광역시 서구 | 15108983 | 대전광역시 서구_상권별 연령별 주민등록인구 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 대전광역시 서구 | 15108982 | 대전광역시 서구_상권별 연월별 유동인구 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 대전광역시 서구 | 15108971 | 대전광역시 서구_상권별 연령대별 유동인구 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 전라남도 | 15111780 | 전라남도_전남관광플랫폼(J-TaaS)의 음식관광 정보 확대를 위한 음식점 DB OPEN API | 문화관광 | JSON | 자동승인 | 심의승인 | 2025-07-21 |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
