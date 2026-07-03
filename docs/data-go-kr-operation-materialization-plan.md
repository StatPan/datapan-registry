# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T18:58:36Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `10199` (`84.6%`)
- APIs without operation mapping: `1861`
- Planned institutions: `10`
- Planned APIs: `612`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 충청남도 | 96 | 69 | 27 | 71.9% | 27 | 0 |
| 3 | 한국도로공사 | 93 | 0 | 93 | 0.0% | 93 | 0 |
| 4 | 기상청 | 89 | 44 | 45 | 49.4% | 45 | 0 |
| 5 | 농림축산식품부 | 87 | 1 | 86 | 1.1% | 86 | 0 |
| 6 | 문화체육관광부 | 86 | 1 | 85 | 1.2% | 85 | 0 |
| 7 | 한국수자원공사 | 84 | 79 | 5 | 94.0% | 5 | 0 |
| 8 | 금융감독원 | 83 | 0 | 83 | 0.0% | 83 | 0 |
| 9 | 대전광역시 | 81 | 53 | 28 | 65.4% | 28 | 0 |
| 10 | 서울특별시 | 79 | 11 | 68 | 13.9% | 68 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 충청남도 | 15096617 | 충청남도_충남넷_공모전공지사항 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-01-31 |
| 충청남도 | 15096616 | 충청남도_충남넷_공모일정 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-01-31 |
| 충청남도 | 15096615 | 충청남도_충남넷_수출입동향 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-01-31 |
| 충청남도 | 15096614 | 충청남도_충남넷_정책실명제 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-01-31 |
| 충청남도 | 15096613 | 충청남도_충남넷_학술용역 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-01-31 |
| 한국도로공사 | 15155538 | 한국도로공사_영업소 하이패스 차로 개폐 현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-12-01 |
| 한국도로공사 | 15062048 | 한국도로공사_공간정보노선현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-08-21 |
| 한국도로공사 | 15128076 | 한국도로공사_전자조달 계약공개현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-08-11 |
| 한국도로공사 | 15111645 | 한국도로공사_영업소 차로 운영현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-08-11 |
| 한국도로공사 | 15111644 | 한국도로공사_영업소간 통행요금 조회 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-08-11 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
