import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Asset } from "./api";

/**
 * 蒙版画布：在原图上涂白 = 要重绘的区域，橡皮擦掉 = 保留原样。
 *
 * 导出的是与原图同尺寸的**黑白 PNG**（白 = 255 = 重绘），这正是 ComfyUI
 * `LoadImageMask` 取 red 通道后得到的东西，所以后端不需要做任何格式转换。
 * 每画完一笔停顿 0.5 秒上传一次，表单里存的是 asset_id（和图片输入同一套素材机制）。
 */
export function MaskEditor({
  source,
  assetId,
  onChange,
}: {
  /** 要涂的原图（image 参数已上传的素材） */
  source?: Asset;
  /** 已上传的蒙版素材 id */
  assetId?: string;
  onChange: (id: string) => void;
}) {
  const viewRef = useRef<HTMLCanvasElement>(null);
  // mask：黑白底图（导出用）；red：红色笔迹层（屏幕上看的那层）
  const maskRef = useRef<HTMLCanvasElement | null>(null);
  const redRef = useRef<HTMLCanvasElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const painting = useRef(false);
  const lastDot = useRef<{ x: number; y: number } | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const [brush, setBrush] = useState(48);
  const [erase, setErase] = useState(false);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<string | null>(null);

  const redraw = useCallback(() => {
    const view = viewRef.current;
    const img = imgRef.current;
    const red = redRef.current;
    if (!view || !img || !red) return;
    const ctx = view.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, view.width, view.height);
    ctx.drawImage(img, 0, 0);
    ctx.globalAlpha = 0.55;
    ctx.drawImage(red, 0, 0);
    ctx.globalAlpha = 1;
  }, []);

  // 换原图就重建两张离屏画布（尺寸跟着原图的真实像素走，保证和 image 严格对齐）
  useEffect(() => {
    if (!source) {
      setReady(false);
      imgRef.current = null;
      maskRef.current = null;
      redRef.current = null;
      return;
    }
    const img = new Image();
    img.onload = () => {
      const view = viewRef.current;
      if (!view) return;
      const w = img.naturalWidth;
      const h = img.naturalHeight;
      view.width = w;
      view.height = h;

      const mask = document.createElement("canvas");
      mask.width = w;
      mask.height = h;
      const mctx = mask.getContext("2d");
      if (!mctx) return;
      mctx.fillStyle = "#000";
      mctx.fillRect(0, 0, w, h);

      const red = document.createElement("canvas");
      red.width = w;
      red.height = h;

      imgRef.current = img;
      maskRef.current = mask;
      redRef.current = red;
      setReady(true);
      redraw();
      // 换图就把上一张的蒙版作废，否则会把旧蒙版套到新图上（尺寸还对不上）
      onChange("");
    };
    img.onerror = () => setHint("原图加载失败，重新上传一次");
    img.src = source.url;
  }, [source, redraw]);

  /** 把 mask 画布导成 PNG 上传；连续涂抹只会在停手 0.5 秒后传最后一次 */
  const scheduleUpload = useCallback(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(async () => {
      const mask = maskRef.current;
      if (!mask) return;
      setBusy(true);
      try {
        const blob = await new Promise<Blob | null>((resolve) =>
          mask.toBlob(resolve, "image/png")
        );
        if (!blob) throw new Error("蒙版导出失败");
        const asset = await api.uploadAsset(new File([blob], "mask.png", { type: "image/png" }));
        onChange(asset.id);
        setHint(null);
      } catch (e) {
        setHint(`蒙版上传失败：${String(e)}`);
      } finally {
        setBusy(false);
      }
    }, 500);
  }, [onChange]);

  // 卸载时清掉待执行的上传，避免对已切走的变体写表单
  useEffect(() => () => window.clearTimeout(timer.current), []);

  /** 屏幕坐标 → 原图坐标（画布 CSS 尺寸和像素尺寸不是一个量级） */
  function toImage(e: React.PointerEvent<HTMLCanvasElement>) {
    const view = viewRef.current;
    if (!view) return { x: 0, y: 0 };
    const rect = view.getBoundingClientRect();
    return {
      x: ((e.clientX - rect.left) / rect.width) * view.width,
      y: ((e.clientY - rect.top) / rect.height) * view.height,
    };
  }

  function paint(from: { x: number; y: number }, to: { x: number; y: number }) {
    const mask = maskRef.current;
    const red = redRef.current;
    if (!mask || !red) return;
    // 两张画布同步画：mask 记事实（黑/白），red 只负责好看的红色高亮
    const layers: [CanvasRenderingContext2D | null, string, GlobalCompositeOperation][] = [
      [mask.getContext("2d"), erase ? "#000" : "#fff", "source-over"],
      [red.getContext("2d"), "#ff5252", erase ? "destination-out" : "source-over"],
    ];
    for (const [ctx, color, composite] of layers) {
      if (!ctx) continue;
      ctx.save();
      ctx.globalCompositeOperation = composite;
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = brush;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
      ctx.stroke();
      ctx.restore();
    }
    redraw();
  }

  function dot(at: { x: number; y: number }) {
    const mask = maskRef.current;
    const red = redRef.current;
    if (!mask || !red) return;
    for (const [ctx, color, composite] of [
      [mask.getContext("2d"), erase ? "#000" : "#fff", "source-over"],
      [red.getContext("2d"), "#ff5252", erase ? "destination-out" : "source-over"],
    ] as [CanvasRenderingContext2D | null, string, GlobalCompositeOperation][]) {
      if (!ctx) continue;
      ctx.save();
      ctx.globalCompositeOperation = composite;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(at.x, at.y, brush / 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
    redraw();
  }

  if (!source) {
    return <p className="hint small">先在上面上传原图，再在这里涂抹要重绘的区域</p>;
  }

  return (
    <div className="mask-editor">
      <canvas
        ref={viewRef}
        className="mask-canvas"
        onPointerDown={(e) => {
          if (!ready) return;
          e.currentTarget.setPointerCapture(e.pointerId);
          painting.current = true;
          const p = toImage(e);
          lastDot.current = p;
          dot(p);
        }}
        onPointerMove={(e) => {
          if (!painting.current || !ready) return;
          const p = toImage(e);
          if (lastDot.current) paint(lastDot.current, p);
          lastDot.current = p;
        }}
        onPointerUp={() => {
          if (!painting.current) return;
          painting.current = false;
          lastDot.current = null;
          scheduleUpload();
        }}
        onPointerCancel={() => {
          painting.current = false;
          lastDot.current = null;
        }}
      />
      <div className="mask-tools">
        <button
          type="button"
          className={erase ? "ghost" : "ghost on"}
          onClick={() => setErase(false)}
        >
          画笔
        </button>
        <button
          type="button"
          className={erase ? "ghost on" : "ghost"}
          onClick={() => setErase(true)}
        >
          橡皮
        </button>
        <label className="mask-brush">
          笔刷 {brush}px
          <input
            type="range"
            min={8}
            max={256}
            step={8}
            value={brush}
            onChange={(e) => setBrush(Number(e.target.value))}
          />
        </label>
        <button
          type="button"
          className="ghost"
          onClick={() => {
            const mask = maskRef.current;
            const red = redRef.current;
            if (!mask || !red) return;
            const mctx = mask.getContext("2d");
            const rctx = red.getContext("2d");
            if (!mctx || !rctx) return;
            mctx.fillStyle = "#000";
            mctx.fillRect(0, 0, mask.width, mask.height);
            rctx.clearRect(0, 0, red.width, red.height);
            redraw();
            scheduleUpload();
          }}
        >
          清空
        </button>
        <span className="hint small">
          {busy ? "上传蒙版…" : hint ?? (assetId ? "蒙版已就绪" : "涂白 = 重绘区域")}
        </span>
      </div>
    </div>
  );
}
