# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-02T21:00:31Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `7215` (`59.8%`)
- APIs without operation mapping: `4845`
- Planned institutions: `10`
- Planned APIs: `645`
- First queue: `행정안전부`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 행정안전부 | 1252 | 618 | 634 | 49.4% | 100 | 534 |
| 2 | 경기도 | 840 | 13 | 827 | 1.5% | 100 | 727 |
| 3 | 국토교통부 | 393 | 127 | 266 | 32.3% | 100 | 166 |
| 4 | 식품의약품안전처 | 392 | 272 | 120 | 69.4% | 100 | 20 |
| 5 | 국회 국회사무처 | 277 | 0 | 277 | 0.0% | 100 | 177 |
| 6 | 성평등가족부 | 273 | 272 | 1 | 99.6% | 1 | 0 |
| 7 | 공정거래위원회 | 250 | 216 | 34 | 86.4% | 34 | 0 |
| 8 | 한국산업인력공단 | 230 | 228 | 2 | 99.1% | 2 | 0 |
| 9 | 국립암센터 | 212 | 204 | 8 | 96.2% | 8 | 0 |
| 10 | 법제처 | 203 | 5 | 198 | 2.5% | 100 | 98 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 행정안전부 | 15154047 | 행정안전부_국보 | 문화관광 | JSON | 자동승인 | 심의승인 | 2025-11-25 |
| 행정안전부 | 15154023 | 행정안전부_국내전염병발생현황 | 농축수산 | JSON | 자동승인 | 심의승인 | 2025-11-25 |
| 행정안전부 | 15154020 | 행정안전부_국가지점번호정보데이터서비스 | 교통물류 | JSON | 자동승인 | 심의승인 | 2025-11-25 |
| 행정안전부 | 15154017 | 행정안전부_국가민속문화유산 | 문화관광 | JSON | 자동승인 | 심의승인 | 2025-11-25 |
| 행정안전부 | 15154015 | 행정안전부_국가무형유산 | 문화관광 | JSON | 자동승인 | 심의승인 | 2025-11-25 |
| 경기도 | 15057625 | 경기도_물놀이형수경시설 설치운영신고 현황 | 환경기상 | XML | 자동승인 | 자동승인 | 2025-12-05 |
| 경기도 | 15056758 | 경기도_물놀이형 수경시설 관리실태 점검결과 | 환경기상 | XML | 자동승인 | 자동승인 | 2025-12-05 |
| 경기도 | 15121898 | 경기도_상시속도 | 교통물류 | XML | 자동승인 | 심의승인 | 2025-09-25 |
| 경기도 | 15121891 | 경기도_상시교통량(고속도로) | 교통물류 | XML | 자동승인 | 심의승인 | 2025-09-25 |
| 경기도 | 15121882 | 경기도_시군별 버스 이용객수 | 교통물류 | XML | 자동승인 | 심의승인 | 2025-09-25 |
| 국토교통부 | 15067164 | 국토교통부_건설보고서 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15067161 | 국토교통부_건설 기술사례정보 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061112 | 국토교통부_건설공사 원가절감사례 목록 조회 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061101 | 국토교통부_설계VE 상세 VE제안목록 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061098 | 국토교통부_품질검사전문기관 상세정보 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
