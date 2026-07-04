# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-04T04:17:10Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `11457` (`95.0%`)
- APIs without operation mapping: `603`
- Planned institutions: `10`
- Planned APIs: `211`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 86 | 1 | 98.9% | 1 | 0 |
| 3 | 주택도시보증공사 | 22 | 0 | 22 | 0.0% | 22 | 0 |
| 4 | 예술경영지원센터 | 21 | 0 | 21 | 0.0% | 21 | 0 |
| 5 | 국가데이터처 | 21 | 10 | 11 | 47.6% | 11 | 0 |
| 6 | 경찰청 | 19 | 9 | 10 | 47.4% | 10 | 0 |
| 7 | 기획예산처 | 18 | 3 | 15 | 16.7% | 15 | 0 |
| 8 | 국방부 | 17 | 0 | 17 | 0.0% | 17 | 0 |
| 9 | 한국은행 | 16 | 0 | 16 | 0.0% | 16 | 0 |
| 10 | 인천광역시 | 16 | 10 | 6 | 62.5% | 6 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 3055105 | 조건불리지역직접지불제 지원현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 주택도시보증공사 | 15070256 | 주택도시보증공사_지역별 ㎡당 분양가격(지역) | 국토관리 | XML | 자동승인 | 심의승인 | 2026-03-26 |
| 주택도시보증공사 | 15011457 | 주택도시보증공사_분양보증현황(지역) | 국토관리 | XML | 자동승인 | 자동승인 | 2026-03-26 |
| 주택도시보증공사 | 15058820 | 주택도시보증공사_㎡당분양가격지수 | 국토관리 | XML | 자동승인 | 자동승인 | 2026-02-11 |
| 주택도시보증공사 | 15143827 | 주택도시보증공사_든든전세 모집공고 | 재정금융 | JSON | 자동승인 | 자동승인 | 2026-01-29 |
| 주택도시보증공사 | 15071173 | 주택도시보증공사_하자보수보증 정보공개 | 국토관리 | XML | 자동승인 | 심의승인 | 2026-01-29 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
