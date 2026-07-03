# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T23:08:59Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `10927` (`90.6%`)
- APIs without operation mapping: `1133`
- Planned institutions: `10`
- Planned APIs: `249`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 서울시설공단 | 45 | 40 | 5 | 88.9% | 5 | 0 |
| 4 | 국립생태원 | 43 | 4 | 39 | 9.3% | 39 | 0 |
| 5 | 해양수산부 국립수산물품질관리원 | 40 | 3 | 37 | 7.5% | 37 | 0 |
| 6 | 서울특별시 동작구 | 38 | 2 | 36 | 5.3% | 36 | 0 |
| 7 | 관세청 | 38 | 36 | 2 | 94.7% | 2 | 0 |
| 8 | 과학기술정보통신부 우정사업본부 | 34 | 33 | 1 | 97.1% | 1 | 0 |
| 9 | 한국사회보장정보원 | 32 | 9 | 23 | 28.1% | 23 | 0 |
| 10 | 대구광역시 | 32 | 19 | 13 | 59.4% | 13 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 서울시설공단 | 15057705 | 서울시설공단_장애인콜택시 일별 이용현황 | 사회복지 | JSON+XML | 자동승인 | 자동승인 | 2025-06-10 |
| 서울시설공단 | 15057474 | 서울시설공단_장애인콜택시 목적지Best100 | 사회복지 | JSON+XML | 자동승인 | 자동승인 | 2025-06-10 |
| 서울시설공단 | 15130221 | 서울시설공단_공공자전거 대여소별 이용정보 | 교통물류 | JSON | 자동승인 | 심의승인 | 2025-06-05 |
| 서울시설공단 | 15126330 | 서울시설공단_공공자전거 대여이력 정보 | 교통물류 | JSON | 자동승인 | 심의승인 | 2025-06-05 |
| 서울시설공단 | 15118599 | 서울시설공단_공공자전거 대여소 현황 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-06-05 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
