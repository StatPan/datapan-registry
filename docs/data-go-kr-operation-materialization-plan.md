# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T16:35:13Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `9392` (`77.9%`)
- APIs without operation mapping: `2668`
- Planned institutions: `10`
- Planned APIs: `604`
- First queue: `한국산업인력공단`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 한국산업인력공단 | 230 | 228 | 2 | 99.1% | 2 | 0 |
| 2 | 국립암센터 | 212 | 204 | 8 | 96.2% | 8 | 0 |
| 3 | 법제처 | 203 | 5 | 198 | 2.5% | 100 | 98 |
| 4 | 경기도 광명시 | 197 | 0 | 197 | 0.0% | 100 | 97 |
| 5 | 해양수산부 | 173 | 172 | 1 | 99.4% | 1 | 0 |
| 6 | 제주특별자치도 | 171 | 18 | 153 | 10.5% | 100 | 53 |
| 7 | 농촌진흥청 | 136 | 14 | 122 | 10.3% | 100 | 22 |
| 8 | 대전광역시 서구 | 125 | 0 | 125 | 0.0% | 100 | 25 |
| 9 | 전라남도 | 109 | 108 | 1 | 99.1% | 1 | 0 |
| 10 | 울산항만공사 | 98 | 6 | 92 | 6.1% | 92 | 0 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 한국산업인력공단 | 3045136 | [산업인력] 해외취업 통계정보 | 산업고용 | XML | 자동승인 | 자동승인 | 2025-06-11 |
| 한국산업인력공단 | 3038249 | 한국산업인력공단_해외진출정보 | 산업고용 | XML | 자동승인 | 자동승인 | 2018-05-02 |
| 국립암센터 | 15122235 | 국립암센터_국가암정보센터 내가 알고 싶은 암(100대암) | 보건의료 | TEXT | 자동승인 | 심의승인 | 2025-05-29 |
| 국립암센터 | 15122232 | 국립암센터_국가암정보센터 암정보사전 | 보건의료 | TEXT | 자동승인 | 심의승인 | 2025-05-29 |
| 국립암센터 | 15122229 | 국립암센터_국가암정보센터 게시물자료 | 보건의료 | TEXT | 자동승인 | 심의승인 | 2025-05-29 |
| 국립암센터 | 15122227 | 국립암센터_국가암정보센터 암환자 생활백서 | 보건의료 | TEXT | 자동승인 | 심의승인 | 2025-05-29 |
| 국립암센터 | 15122222 | 국립암센터_국가암정보센터 암예방과 검진 질문과 답변 | 보건의료 | TEXT | 자동승인 | 심의승인 | 2025-05-29 |
| 법제처 | 15157096 | 법제처_법령정보지식베이스 지능형 법령검색 시스템 연관법령 | 공공행정 | XML | 자동승인 | 심의승인 | 2026-01-09 |
| 법제처 | 15157095 | 법제처_법령정보지식베이스 지능형 법령검색 시스템 검색 | 공공행정 | XML | 자동승인 | 심의승인 | 2026-01-09 |
| 법제처 | 15090746 | 법제처_법령해석례 상세 조회 정보 | 공공행정 | XML | 자동승인 | 심의승인 | 2026-01-02 |
| 법제처 | 15153083 | 법제처_법제처 법령해석 본문 조회 | 공공행정 | JSON+XML | 자동승인 | 심의승인 | 2025-12-04 |
| 법제처 | 15153080 | 법제처_법제처 법령해석 목록 조회 | 공공행정 | JSON+XML | 자동승인 | 심의승인 | 2025-12-04 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
