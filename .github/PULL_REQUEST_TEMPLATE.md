## 做了什麼

<!-- 一兩句話 -->

Closes #

## 怎麼驗證

```bash
cd web && pnpm build && pnpm lint && pnpm test:unit && cd ..
uv run pytest -q tests/test_web.py tests/test_ui_state.py tests/test_contracts.py tests/test_schema_parity.py
```

<!-- 貼最後一行結果。PR 上的 CI（.github/workflows/ci.yml）會自動跑同一組，py、web 兩個 job 都要綠 -->

## 檢查清單

- [ ] 上面的測試綠，CI 的 py、web 兩個 job 也綠
- [ ] 附 1440 和 390 寬的前後截圖（改到 web/ 時）
- [ ] 只改了自己座位的資料夾（或已在 issue 說明）
- [ ] 沒有提交 data/runtime/、.env、金鑰或 PIN
- [ ] 沒有加任何能動到機台的工具
- [ ] 改了 warroom/contracts.py 的話，web/src/contracts.ts 也同步了
- [ ] 文件或投影片裡的新數字有 bench/results/ 的檔案
