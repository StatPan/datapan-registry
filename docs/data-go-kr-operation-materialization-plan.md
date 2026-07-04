# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-04T02:17:16Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `11262` (`93.4%`)
- APIs without operation mapping: `798`
- Planned institutions: `10`
- Planned APIs: `250`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 국가철도공단 | 26 | 0 | 26 | 0.0% | 26 | 0 |
| 4 | 대전광역시 유성구 | 26 | 0 | 26 | 0.0% | 26 | 0 |
| 5 | 한국체육산업개발주식회사 | 26 | 4 | 22 | 15.4% | 22 | 0 |
| 6 | 한국연구재단 | 26 | 24 | 2 | 92.3% | 2 | 0 |
| 7 | 한국과학기술정보연구원 | 25 | 0 | 25 | 0.0% | 25 | 0 |
| 8 | 한국산업기술기획평가원 | 25 | 0 | 25 | 0.0% | 25 | 0 |
| 9 | 농림축산식품부 농림축산검역본부 | 25 | 8 | 17 | 32.0% | 17 | 0 |
| 10 | 서울교통공사 | 25 | 11 | 14 | 44.0% | 14 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 국가철도공단 | 15041666 | 국가철도공단_도시철도 전체노선정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-11-24 |
| 국가철도공단 | 15057421 | 국가철도공단_역사별 소화설비 현황 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-11-17 |
| 국가철도공단 | 15056590 | 국가철도공단_역사별 공기호흡기 현황 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-11-17 |
| 국가철도공단 | 15041692 | 국가철도공단_역사별 장애인 화장실 위치 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-11-17 |
| 국가철도공단 | 15041691 | 국가철도공단_역사별 인접 승강기 차량번호 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-11-17 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
