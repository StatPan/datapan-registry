# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T13:33:15Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `8353` (`69.3%`)
- APIs without operation mapping: `3707`
- Planned institutions: `10`
- Planned APIs: `645`
- First queue: `경기도`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 경기도 | 840 | 499 | 341 | 59.4% | 100 | 241 |
| 2 | 국토교통부 | 393 | 127 | 266 | 32.3% | 100 | 166 |
| 3 | 식품의약품안전처 | 392 | 272 | 120 | 69.4% | 100 | 20 |
| 4 | 국회 국회사무처 | 277 | 0 | 277 | 0.0% | 100 | 177 |
| 5 | 성평등가족부 | 273 | 272 | 1 | 99.6% | 1 | 0 |
| 6 | 공정거래위원회 | 250 | 216 | 34 | 86.4% | 34 | 0 |
| 7 | 한국산업인력공단 | 230 | 228 | 2 | 99.1% | 2 | 0 |
| 8 | 국립암센터 | 212 | 204 | 8 | 96.2% | 8 | 0 |
| 9 | 법제처 | 203 | 5 | 198 | 2.5% | 100 | 98 |
| 10 | 경기도 광명시 | 197 | 0 | 197 | 0.0% | 100 | 97 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 경기도 | 15056594 | 경기도_종돈업 현황 | 농축수산 | XML | 자동승인 | 자동승인 | 2025-09-05 |
| 경기도 | 15056584 | 경기도_일자리센터 현황 | 산업고용 | XML | 자동승인 | 자동승인 | 2025-09-05 |
| 경기도 | 15058990 | 경기도_온천 현황 | 문화관광 | XML | 자동승인 | 자동승인 | 2025-09-04 |
| 경기도 | 15058563 | 경기도_일반게임 제공업체 현황 | 문화관광 | XML | 자동승인 | 자동승인 | 2025-09-04 |
| 경기도 | 15057830 | 경기도_인터넷 컴퓨터게임 시설제공업체 현황 | 문화관광 | XML | 자동승인 | 자동승인 | 2025-09-04 |
| 국토교통부 | 15067164 | 국토교통부_건설보고서 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15067161 | 국토교통부_건설 기술사례정보 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061112 | 국토교통부_건설공사 원가절감사례 목록 조회 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061101 | 국토교통부_설계VE 상세 VE제안목록 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061098 | 국토교통부_품질검사전문기관 상세정보 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 식품의약품안전처 | 15064859 | 식품의약품안전처_식품접객업정보 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2026-03-26 |
| 식품의약품안전처 | 15091535 | 식품의약품안전처_수입식품업 폐업정보 | 식품건강 | JSON+XML | 자동승인 | 심의승인 | 2025-09-19 |
| 식품의약품안전처 | 15111830 | 식품의약품안전처_지하수 수질측정망 측정결과 | 식품건강 | XML | 자동승인 | 심의승인 | 2025-08-27 |
| 식품의약품안전처 | 15111829 | 식품의약품안전처_토양지하수 토양실태조사정보 | 식품건강 | XML | 자동승인 | 심의승인 | 2025-08-27 |
| 식품의약품안전처 | 15111816 | 식품의약품안전처_어류질병정보 | 식품건강 | XML | 자동승인 | 심의승인 | 2025-08-27 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
