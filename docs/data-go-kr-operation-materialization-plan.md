# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-04T01:17:09Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `11153` (`92.5%`)
- APIs without operation mapping: `907`
- Planned institutions: `10`
- Planned APIs: `276`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 제주특별자치도 서귀포시 | 28 | 0 | 28 | 0.0% | 28 | 0 |
| 4 | 서울특별시농수산식품공사 | 28 | 1 | 27 | 3.6% | 27 | 0 |
| 5 | 국가유산청 국립문화유산연구원 | 27 | 0 | 27 | 0.0% | 27 | 0 |
| 6 | 경기도 안양시 | 27 | 1 | 26 | 3.7% | 26 | 0 |
| 7 | 대전교통공사 | 27 | 26 | 1 | 96.3% | 1 | 0 |
| 8 | 국가철도공단 | 26 | 0 | 26 | 0.0% | 26 | 0 |
| 9 | 대전광역시 유성구 | 26 | 0 | 26 | 0.0% | 26 | 0 |
| 10 | 한국체육산업개발주식회사 | 26 | 4 | 22 | 15.4% | 22 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 제주특별자치도 서귀포시 | 15138749 | 제주특별자치도 서귀포시_우리동네건강시설 | 보건의료 | XML | 자동승인 | 심의승인 | 2024-10-16 |
| 제주특별자치도 서귀포시 | 15138748 | 제주특별자치도 서귀포시_어린이건강놀이터 | 보건의료 | XML | 자동승인 | 심의승인 | 2024-10-16 |
| 제주특별자치도 서귀포시 | 15065438 | 제주특별자치도 서귀포시_서귀포시 자기주도학습센터 | 교육 | XML | 자동승인 | 심의승인 | 2024-04-25 |
| 제주특별자치도 서귀포시 | 15065435 | 제주특별자치도 서귀포시_서귀포시 인구현황 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-04-25 |
| 제주특별자치도 서귀포시 | 15065433 | 제주특별자치도 서귀포시_서귀포시 알림마당 | 공공행정 | XML | 자동승인 | 심의승인 | 2024-04-25 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
