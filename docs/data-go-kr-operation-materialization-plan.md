# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-02T21:37:18Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `7414` (`61.5%`)
- APIs without operation mapping: `4646`
- Planned institutions: `10`
- Planned APIs: `645`
- First queue: `행정안전부`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 행정안전부 | 1252 | 793 | 459 | 63.3% | 100 | 359 |
| 2 | 경기도 | 840 | 19 | 821 | 2.3% | 100 | 721 |
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
| 행정안전부 | 15153491 | 행정안전부_전통시장안전점검결과 | 재난안전 | JSON | 자동승인 | 심의승인 | 2025-11-21 |
| 행정안전부 | 15153441 | 행정안전부_유해화학물질취급시설안전점검결과 | 재난안전 | JSON | 자동승인 | 심의승인 | 2025-11-21 |
| 행정안전부 | 15153433 | 행정안전부_여객선안전점검결과 | 재난안전 | JSON | 자동승인 | 심의승인 | 2025-11-21 |
| 행정안전부 | 15153429 | 행정안전부_어린이놀이시설안전점검결과 | 재난안전 | JSON | 자동승인 | 심의승인 | 2025-11-21 |
| 행정안전부 | 15101860 | 행정안전부_생활안전지도 어린이 아토피 | 보건의료 | WMS | 자동승인 | 심의승인 | 2025-11-21 |
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
