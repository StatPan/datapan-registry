# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T19:14:02Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `10226` (`84.8%`)
- APIs without operation mapping: `1834`
- Planned institutions: `10`
- Planned APIs: `651`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 한국도로공사 | 93 | 0 | 93 | 0.0% | 93 | 0 |
| 3 | 기상청 | 89 | 44 | 45 | 49.4% | 45 | 0 |
| 4 | 농림축산식품부 | 87 | 1 | 86 | 1.1% | 86 | 0 |
| 5 | 문화체육관광부 | 86 | 1 | 85 | 1.2% | 85 | 0 |
| 6 | 한국수자원공사 | 84 | 79 | 5 | 94.0% | 5 | 0 |
| 7 | 금융감독원 | 83 | 0 | 83 | 0.0% | 83 | 0 |
| 8 | 대전광역시 | 81 | 53 | 28 | 65.4% | 28 | 0 |
| 9 | 서울특별시 | 79 | 11 | 68 | 13.9% | 68 | 0 |
| 10 | 농림수산식품교육문화정보원 | 75 | 9 | 66 | 12.0% | 66 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 한국도로공사 | 15155538 | 한국도로공사_영업소 하이패스 차로 개폐 현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-12-01 |
| 한국도로공사 | 15062048 | 한국도로공사_공간정보노선현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-08-21 |
| 한국도로공사 | 15128076 | 한국도로공사_전자조달 계약공개현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-08-11 |
| 한국도로공사 | 15111645 | 한국도로공사_영업소 차로 운영현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-08-11 |
| 한국도로공사 | 15111644 | 한국도로공사_영업소간 통행요금 조회 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-08-11 |
| 기상청 | 15159045 | 기상청_도로기상관측자료 | 환경기상 | TEXT | 자동승인 | 심의승인 | 2026-04-14 |
| 기상청 | 15159041 | 기상청_도로위험기상정보 | 환경기상 | TEXT | 자동승인 | 심의승인 | 2026-04-14 |
| 기상청 | 15139470 | 기상청_단기예보 조회서비스(기상청API허브 연계) | 환경기상 | TEXT | 자동승인 | 심의승인 | 2026-03-26 |
| 기상청 | 15139478 | 기상청_고해상도 격자 조회서비스 | 환경기상 | TEXT | 자동승인 | 심의승인 | 2026-01-20 |
| 기상청 | 15139439 | 기상청_지상기상관측 지점정보 조회서비스 | 환경기상 | TEXT | 자동승인 | 심의승인 | 2025-12-04 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
