# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T17:58:13Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `9898` (`82.1%`)
- APIs without operation mapping: `2162`
- Planned institutions: `10`
- Planned APIs: `682`
- First queue: `제주특별자치도`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 제주특별자치도 | 171 | 118 | 53 | 69.0% | 53 | 0 |
| 2 | 농촌진흥청 | 136 | 14 | 122 | 10.3% | 100 | 22 |
| 3 | 대전광역시 서구 | 125 | 0 | 125 | 0.0% | 100 | 25 |
| 4 | 전라남도 | 109 | 108 | 1 | 99.1% | 1 | 0 |
| 5 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |
| 6 | 충청남도 | 96 | 69 | 27 | 71.9% | 27 | 0 |
| 7 | 한국도로공사 | 93 | 0 | 93 | 0.0% | 93 | 0 |
| 8 | 기상청 | 89 | 44 | 45 | 49.4% | 45 | 0 |
| 9 | 농림축산식품부 | 87 | 1 | 86 | 1.1% | 86 | 0 |
| 10 | 문화체육관광부 | 86 | 1 | 85 | 1.2% | 85 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 제주특별자치도 | 15094859 | 제주특별자치도_내국인 관광객 테마별 카드 이용 데이터 | 문화관광 | JSON | 자동승인 | 심의승인 | 2021-11-17 |
| 제주특별자치도 | 15094858 | 제주특별자치도_내국인 관광객 업종별 카드 이용데이터 | 문화관광 | JSON | 자동승인 | 심의승인 | 2021-11-17 |
| 제주특별자치도 | 15094857 | 제주특별자치도_내국인 관광객 지역, 업종, 성별, 연령대별 카드 이용 데이터 | 문화관광 | JSON | 자동승인 | 심의승인 | 2021-11-17 |
| 제주특별자치도 | 15094854 | 제주특별자치도_와이파이 월간 사용자수 | 과학기술 | JSON | 자동승인 | 심의승인 | 2021-11-17 |
| 제주특별자치도 | 15094851 | 제주특별자치도_와이파이 일일 사용자수 | 과학기술 | JSON | 자동승인 | 심의승인 | 2021-11-17 |
| 농촌진흥청 | 3060744 | 농촌진흥청_주간농사정보 | 농축수산 | XML | 자동승인 | 자동승인 | 2026-04-29 |
| 농촌진흥청 | 15101991 | 농촌진흥청_유기농실천사례 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-04-29 |
| 농촌진흥청 | 15101989 | 농촌진흥청_품목별 수출정보 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-04-29 |
| 농촌진흥청 | 15086548 | 농촌진흥청_고문헌 검색 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-04-29 |
| 농촌진흥청 | 15086547 | 농촌진흥청_곤충표본검색 | 농축수산 | XML | 자동승인 | 심의승인 | 2026-04-29 |
| 대전광역시 서구 | 15109957 | 대전광역시 서구_행정동별 연령대별 유동인구 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 대전광역시 서구 | 15108988 | 대전광역시 서구_행정동별 업종별 착한가격업소 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 대전광역시 서구 | 15108983 | 대전광역시 서구_상권별 연령별 주민등록인구 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 대전광역시 서구 | 15108982 | 대전광역시 서구_상권별 연월별 유동인구 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |
| 대전광역시 서구 | 15108971 | 대전광역시 서구_상권별 연령대별 유동인구 현황 | 공공행정 | JSON | 자동승인 | 심의승인 | 2025-11-24 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
