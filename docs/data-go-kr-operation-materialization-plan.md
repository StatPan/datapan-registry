# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T19:33:03Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `10364` (`85.9%`)
- APIs without operation mapping: `1696`
- Planned institutions: `10`
- Planned APIs: `582`
- First queue: `울산항만공사`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 2 | 농림축산식품부 | 87 | 1 | 86 | 1.1% | 86 | 0 |
| 3 | 문화체육관광부 | 86 | 1 | 85 | 1.2% | 85 | 0 |
| 4 | 한국수자원공사 | 84 | 79 | 5 | 94.0% | 5 | 0 |
| 5 | 금융감독원 | 83 | 0 | 83 | 0.0% | 83 | 0 |
| 6 | 대전광역시 | 81 | 53 | 28 | 65.4% | 28 | 0 |
| 7 | 서울특별시 | 79 | 11 | 68 | 13.9% | 68 | 0 |
| 8 | 농림수산식품교육문화정보원 | 75 | 9 | 66 | 12.0% | 66 | 0 |
| 9 | 해양수산부 국립해양조사원 | 68 | 50 | 18 | 73.5% | 18 | 0 |
| 10 | 지식재산처 | 67 | 16 | 51 | 23.9% | 51 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 울산항만공사 | 15056839 | 울산항만공사_부두별/품목별 화물처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15035878 | 울산항만공사_부두별 처리 정보 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-09-19 |
| 울산항만공사 | 15059441 | 울산항만공사_연도별 전국 항만 화물 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059379 | 울산항만공사_지역별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 울산항만공사 | 15059217 | 울산항만공사_국가별 수출입 컨테이너 처리 실적 | 교통물류 | JSON+XML | 자동승인 | 자동승인 | 2025-07-25 |
| 농림축산식품부 | 15157585 | 농림축산식품부_한우 인공수정 기간별 조회 | 농축수산 | JSON | 자동승인 | 심의승인 | 2026-03-10 |
| 농림축산식품부 | 15157584 | 농림축산식품부_한우 인공수정 개체별 조회 | 농축수산 | JSON | 자동승인 | 심의승인 | 2026-03-10 |
| 농림축산식품부 | 3076483 | 국내 외식기업 해외진출 현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 농림축산식품부 | 3076482 | 우수외식업지구 식당현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 농림축산식품부 | 3068147 | 토양개량제 지원사업 현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-11 |
| 문화체육관광부 | 15059312 | 문화체육관광부_유물정보(국립대구박물관) | 문화관광 | XML | 자동승인 | 자동승인 | 2025-12-05 |
| 문화체육관광부 | 15059201 | 문화체육관광부_공연정보(국립민속국악원) | 문화관광 | XML | 자동승인 | 자동승인 | 2025-12-05 |
| 문화체육관광부 | 15058956 | 문화체육관광부_유물정보(국립춘천박물관) | 문화관광 | XML | 자동승인 | 자동승인 | 2025-12-05 |
| 문화체육관광부 | 15057615 | 문화체육관광부_도서 정보(한국영상자료원) | 문화관광 | XML | 자동승인 | 자동승인 | 2025-12-05 |
| 문화체육관광부 | 15057388 | 문화체육관광부_시나리오 정보(한국영상자료원) | 문화관광 | XML | 자동승인 | 자동승인 | 2025-12-05 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
