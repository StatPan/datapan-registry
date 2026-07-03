# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T22:47:17Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `10920` (`90.5%`)
- APIs without operation mapping: `1140`
- Planned institutions: `10`
- Planned APIs: `220`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 한국서부발전(주) | 50 | 44 | 6 | 88.0% | 6 | 0 |
| 4 | 기후에너지환경부 국립환경과학원 | 48 | 47 | 1 | 97.9% | 1 | 0 |
| 5 | 서울시설공단 | 45 | 40 | 5 | 88.9% | 5 | 0 |
| 6 | 국립생태원 | 43 | 4 | 39 | 9.3% | 39 | 0 |
| 7 | 해양수산부 국립수산물품질관리원 | 40 | 3 | 37 | 7.5% | 37 | 0 |
| 8 | 서울특별시 동작구 | 38 | 2 | 36 | 5.3% | 36 | 0 |
| 9 | 관세청 | 38 | 36 | 2 | 94.7% | 2 | 0 |
| 10 | 과학기술정보통신부 우정사업본부 | 34 | 33 | 1 | 97.1% | 1 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 한국서부발전(주) | 15089752 | 한국서부발전(주)_태안군의항리대기정보 | 산업고용 | JSON | 자동승인 | 심의승인 | 2023-04-27 |
| 한국서부발전(주) | 15090195 | 한국서부발전(주)_태안군대기초대기정보 | 산업고용 | JSON | 자동승인 | 심의승인 | 2021-09-27 |
| 한국서부발전(주) | 15089767 | 한국서부발전(주)_태안군산후리대기정보 | 산업고용 | JSON | 자동승인 | 심의승인 | 2021-09-27 |
| 한국서부발전(주) | 15089766 | 한국서부발전(주)_태안군이원초대기정보 | 산업고용 | JSON | 자동승인 | 심의승인 | 2021-09-27 |
| 한국서부발전(주) | 15089762 | 한국서부발전(주)_태안군안기리대기정보 | 산업고용 | JSON | 자동승인 | 심의승인 | 2021-09-27 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
