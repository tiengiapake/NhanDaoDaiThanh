# Quy trình dịch truyện

## Cấu trúc

```
NhanDaoDaiThanh\
  chuong_*.txt          ← chương gốc tiếng Trung (hiện tại, do downloader ghi)
  output\               ← bản dịch tiếng Việt, mỗi chương một file
  tools\
    dich_truyen.py      ← script dịch
    glossary.json       ← tên riêng / thuật ngữ cố định
    run_dich.bat        ← chạy nhanh trên Windows
```

Khi downloader tải xong, có thể chuyển toàn bộ `chuong_*.txt` vào `input\`
rồi thêm `--input input` khi chạy (script không phụ thuộc tên thư mục).

## Cách chạy

```powershell
# Xem trước các chương cần dịch, không gọi API
python -I tools\dich_truyen.py --dry-run

# Dịch thử 3 chương để duyệt văn phong
python -I tools\dich_truyen.py --limit 3

# Dịch toàn bộ chương còn thiếu
python -I tools\dich_truyen.py

# Dịch một khoảng
python -I tools\dich_truyen.py --start 1 --end 50

# Dịch lại, ghi đè file đã có
python -I tools\dich_truyen.py --force --start 1 --end 3
```

Trên Windows có thể chạy `tools\run_dich.bat --limit 3`.

## Hành vi

- **Không ghi đè**: file output đã có (>= 200 ký tự) thì bỏ qua, trừ khi `--force`.
- **Lỗi một chương**: ghi vào `output\loi_dich.txt`, tăng `output\tien_do_dich.json`,
  rồi chạy tiếp chương sau. Chạy lại cùng lệnh sẽ tự thử lại các chương còn thiếu.
- **Ghi an toàn**: file được ghi tạm rồi đổi tên, nên dừng giữa chừng không tạo file dở.
- **Nhất quán**: `tools\glossary.json` được đưa vào mọi lượt dịch. Thuật ngữ mới do
  model đề xuất được thêm vào mục `auto`; bạn có thể sửa tay bất cứ lúc nào.
- **Kiểm tra chất lượng**: bản dịch còn chữ Hán/dấu câu Trung, quá ngắn, hoặc thiếu
  đoạn sẽ bị từ chối và dịch lại tối đa 3 lần.
- **Tiêu đề**: dạng `Chương N: <tên chương>`; cuối file giữ dòng `Nguồn:` của bản gốc
  (tắt bằng `--no-source`).

## Cấu hình API

Script đọc theo thứ tự:

1. Biến môi trường `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `DICH_MODEL`.
2. Khối `env` trong `~/.claude/settings.json`.

Token không được ghi ra file log hay file output. Có thể đổi model bằng
`--model <tên-model>` hoặc biến `DICH_MODEL`.

## Lưu ý

- Script dịch tuần tự để chương sau kế thừa giọng văn/xưng hô của chương trước.
- Chương bị thiếu trong bộ gốc (ví dụ 437–441, 449) sẽ được báo khi khảo sát;
  cần downloader tải bổ sung trước khi có thể dịch.
- Downloader vẫn đang chạy và ghi thêm chương. Chỉ cần chạy lại script để dịch
  các chương mới.
