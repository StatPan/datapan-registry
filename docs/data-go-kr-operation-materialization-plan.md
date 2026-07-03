# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T22:12:37Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `10853` (`90.0%`)
- APIs without operation mapping: `1207`
- Planned institutions: `10`
- Planned APIs: `248`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 한국환경공단 | 66 | 65 | 1 | 98.5% | 1 | 0 |
| 4 | 한국환경연구원 | 65 | 0 | 65 | 0.0% | 65 | 0 |
| 5 | 충청북도 | 51 | 50 | 1 | 98.0% | 1 | 0 |
| 6 | 한국서부발전(주) | 50 | 44 | 6 | 88.0% | 6 | 0 |
| 7 | 기후에너지환경부 국립환경과학원 | 48 | 47 | 1 | 97.9% | 1 | 0 |
| 8 | 서울시설공단 | 45 | 40 | 5 | 88.9% | 5 | 0 |
| 9 | 국립생태원 | 43 | 4 | 39 | 9.3% | 39 | 0 |
| 10 | 해양수산부 국립수산물품질관리원 | 40 | 3 | 37 | 7.5% | 37 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 한국환경공단 | 15106003 | 한국환경공단_자원순환정보시스템_폐기물통계정보 | 환경기상 | JSON | 자동승인 | 심의승인 | 2024-02-27 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
