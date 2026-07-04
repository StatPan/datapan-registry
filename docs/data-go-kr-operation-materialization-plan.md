# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-04T06:39:24Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `11587` (`96.1%`)
- APIs without operation mapping: `473`
- Planned institutions: `10`
- Planned APIs: `164`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 한국문화정보원 | 15 | 6 | 9 | 40.0% | 9 | 0 |
| 4 | 경상북도 안동시 | 15 | 10 | 5 | 66.7% | 5 | 0 |
| 5 | 서울특별시 서대문구 | 14 | 0 | 14 | 0.0% | 14 | 0 |
| 6 | 인천관광공사 | 14 | 6 | 8 | 42.9% | 8 | 0 |
| 7 | 국가보훈부 | 14 | 8 | 6 | 57.1% | 6 | 0 |
| 8 | 광주교통공사 | 14 | 11 | 3 | 78.6% | 3 | 0 |
| 9 | 교육부 | 13 | 0 | 13 | 0.0% | 13 | 0 |
| 10 | 한국문학번역원 | 13 | 0 | 13 | 0.0% | 13 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 한국문화정보원 | 15059313 | 한국문화정보원_디자인문양 | 문화관광 | XML | 자동승인 | 자동승인 | 2025-11-20 |
| 한국문화정보원 | 3069954 | 한국문화정보원_문화동영상 | 문화관광 | XML | 자동승인 | 자동승인 | 2025-08-13 |
| 한국문화정보원 | 15138904 | 한국문화정보원_카페가 있는 서점데이터 | 문화관광 | XML | 자동승인 | 심의승인 | 2025-08-13 |
| 한국문화정보원 | 15138901 | 한국문화정보원_전국 독립서점 및 운영정보 | 문화관광 | XML | 자동승인 | 심의승인 | 2025-08-13 |
| 한국문화정보원 | 15124909 | 한국문화정보원_전국 관광지 주변 전기차충전소 정보 | 문화관광 | XML | 자동승인 | 심의승인 | 2025-08-13 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
