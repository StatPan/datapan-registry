# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T17:30:15Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `9797` (`81.2%`)
- APIs without operation mapping: `2263`
- Planned institutions: `10`
- Planned APIs: `645`
- First queue: `해양수산부`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 해양수산부 | 173 | 172 | 1 | 99.4% | 1 | 0 |
| 2 | 제주특별자치도 | 171 | 18 | 153 | 10.5% | 100 | 53 |
| 3 | 농촌진흥청 | 136 | 14 | 122 | 10.3% | 100 | 22 |
| 4 | 대전광역시 서구 | 125 | 0 | 125 | 0.0% | 100 | 25 |
| 5 | 전라남도 | 109 | 108 | 1 | 99.1% | 1 | 0 |
| 6 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 7 | 충청남도 | 96 | 69 | 27 | 71.9% | 27 | 0 |
| 8 | 한국도로공사 | 93 | 0 | 93 | 0.0% | 93 | 0 |
| 9 | 기상청 | 89 | 44 | 45 | 49.4% | 45 | 0 |
| 10 | 농림축산식품부 | 87 | 1 | 86 | 1.1% | 86 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 해양수산부 | 15084033 | 해양수산부_선박위치정보(연안AIS) 통계정보 | 농축수산 | JSON | 자동승인 | 심의승인 | 2025-08-05 |
| 제주특별자치도 | 15079829 | 제주특별자치도_제주 C-ITS 교통정보 | 교통물류 | XML | 자동승인 | 심의승인 | 2026-04-06 |
| 제주특별자치도 | 15152721 | 제주특별자치도_제주항일기념관 소장유물 | 문화관광 | JSON | 자동승인 | 심의승인 | 2025-11-18 |
| 제주특별자치도 | 15152720 | 제주특별자치도_제주공익활동지원센터 행사홍보 | 문화관광 | JSON | 자동승인 | 심의승인 | 2025-11-18 |
| 제주특별자치도 | 15152716 | 제주특별자치도_상하수도본부 단수공사안내 | 환경기상 | JSON | 자동승인 | 심의승인 | 2025-11-18 |
| 제주특별자치도 | 15152712 | 제주특별자치도_공공도서관 프로그램 현황 | 교육 | JSON | 자동승인 | 심의승인 | 2025-11-18 |
| 농촌진흥청 | 3060744 | 농촌진흥청_주간농사정보 | 농축수산 | XML | 자동승인 | 자동승인 | 2026-04-29 |
| 농촌진흥청 | 15101991 | 농촌진흥청_유기농실천사례 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-04-29 |
| 농촌진흥청 | 15101989 | 농촌진흥청_품목별 수출정보 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-04-29 |
| 농촌진흥청 | 15086548 | 농촌진흥청_고문헌 검색 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-04-29 |
| 농촌진흥청 | 15086547 | 농촌진흥청_곤충표본검색 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-04-29 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
