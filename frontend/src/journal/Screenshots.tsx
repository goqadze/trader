// A journal trade's screenshots: upload (or paste, or drop) chart images, see them big, delete them. Big photos are
// shrunk in the browser first (a phone's 12 MP photo becomes a ~300 KB JPEG), so uploads stay quick and small.

import { DeleteOutlined, PictureOutlined, UploadOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Card, Image, Input, Popconfirm, Space, Upload } from "antd";
import { useEffect, useState } from "react";
import { journalApi } from "./api";
import { MUTED } from "./catalog";
import type { JournalImage } from "./types";

const AS_IS = ["image/png", "image/jpeg", "image/webp", "image/gif"]; // what the journal keeps
const KEEP_BYTES = 1.5 * 1024 * 1024; // smaller than this (and one of AS_IS): uploaded unchanged
const MAX_SIDE = 2000; // px: a bigger image is scaled down to this

/** The file to upload: unchanged when small, else redrawn as a JPEG at most MAX_SIDE wide or tall. Also turns a
 *  phone's HEIC photo into a JPEG where the browser can read it (Safari). */
export async function shrink(file: File): Promise<Blob> {
  if (AS_IS.includes(file.type) && (file.size <= KEEP_BYTES || file.type === "image/gif")) return file;
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new Error(`${file.name || "That file"} isn't an image this browser can read (PNG, JPEG, WebP or GIF)`);
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#ffffff"; // a transparent PNG gets a white background, not black
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Couldn't convert the image"))), "image/jpeg", 0.85));
}

export default function Screenshots({ tradeId, images, onChange }: { tradeId: number; images: JournalImage[]; onChange: () => void }) {
  const { message } = AntApp.useApp();
  const [busy, setBusy] = useState(false);
  const [caption, setCaption] = useState("");

  const upload = async (files: File[]) => {
    if (!files.length) return;
    setBusy(true);
    try {
      for (const f of files) await journalApi.uploadImage(tradeId, await shrink(f), caption.trim());
      setCaption("");
      message.success(files.length === 1 ? "Screenshot added" : `${files.length} screenshots added`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e), 8);
    } finally {
      setBusy(false);
      onChange();
    }
  };

  // Paste a screenshot anywhere on the page (Ctrl/⌘+V), unless typing in a field
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      const files = [...(e.clipboardData?.files ?? [])].filter((f) => f.type.startsWith("image/"));
      if (files.length) {
        e.preventDefault();
        void upload(files);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  });

  const remove = async (id: number) => {
    try {
      await journalApi.deleteImage(id);
      onChange();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Card size="small" title={<Space><PictureOutlined />Screenshots</Space>}>
      {images.length > 0 && (
        <Image.PreviewGroup>
          <Space wrap size={10} style={{ marginBottom: 12 }}>
            {images.map((img) => (
              <div key={img.id} style={{ width: 150 }}>
                <Image src={journalApi.imageUrl(img.id)} alt={img.caption || "screenshot"} width={150} height={100}
                       style={{ objectFit: "cover", borderRadius: 6, background: "#0f1522" }} />
                <Space size={4} style={{ width: "100%", justifyContent: "space-between", marginTop: 2 }}>
                  <span style={{ fontSize: 12, color: MUTED, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 118 }}
                        title={img.caption}>{img.caption || `${Math.round(img.size / 1024)} KB`}</span>
                  <Popconfirm title="Delete this screenshot?" okText="Delete" okButtonProps={{ danger: true }} onConfirm={() => remove(img.id)}>
                    <Button type="text" size="small" danger icon={<DeleteOutlined />} aria-label="Delete screenshot" />
                  </Popconfirm>
                </Space>
              </div>
            ))}
          </Space>
        </Image.PreviewGroup>
      )}
      <Upload.Dragger
        multiple
        accept="image/*"
        showUploadList={false}
        disabled={busy}
        beforeUpload={(_file, list) => {
          if (_file === list[0]) void upload(list as unknown as File[]);
          return false; // uploaded by hand above, not by the component
        }}
        style={{ padding: 4 }}
      >
        <p style={{ margin: 0 }}><UploadOutlined /> {busy ? "Uploading…" : "Drop a chart here, click to choose, or paste one (Ctrl/⌘+V)"}</p>
        <p style={{ margin: 0, fontSize: 12, color: MUTED }}>The chart at the entry and at the exit are the classic two</p>
      </Upload.Dragger>
      <Input size="small" value={caption} onChange={(e) => setCaption(e.target.value)} maxLength={200}
             placeholder="Caption for the next upload (optional): e.g. entry on the 15m chart" style={{ marginTop: 8 }} />
    </Card>
  );
}
