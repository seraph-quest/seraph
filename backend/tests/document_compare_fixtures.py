"""Supported minimal classic-xref PDF generated without a PDF dependency."""
def invoice_pdf(lines=None):
    lines=lines or ["INVOICE USD","SKU QTY UNIT_PRICE","PEN-01 2 3.50","BOOK-02 1 12.00"]
    content=b"BT /F1 12 Tf 20 180 Td "+b" ".join(b"("+line.encode()+b") Tj 0 -20 Td" for line in lines)+b" ET"
    objects=[b"<< /Type /Catalog /Pages 2 0 R >>",b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",b"<< /Length "+str(len(content)).encode()+b" >>\nstream\n"+content+b"\nendstream"]
    raw=b"%PDF-1.4\n"; offsets=[0]
    for index,value in enumerate(objects,1):
        offsets.append(len(raw));raw+=str(index).encode()+b" 0 obj\n"+value+b"\nendobj\n"
    start=len(raw); raw+=b"xref\n0 6\n0000000000 65535 f \n"+b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    return raw+b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"+str(start).encode()+b"\n%%EOF\n"
