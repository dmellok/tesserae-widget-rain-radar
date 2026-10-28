// rain_radar: the radar around the panel as a printed map.
//
// The server hands over the coast as SVG path data (Natural Earth, in the
// pixels of a square view with home at the centre), the radar as a PNG data
// URI with a transparent background in the same pixels, a strip of
// thumbnails, and the headline. This file lays them out: one SVG per map
// with the coast painted from the theme's tokens and the radar on top.
//
// Two layouts: "strip" (the map in a ruled frame, the last hour as a strip of
// frames beneath it, the title and headline set along the right edge reading
// bottom to top; under the strip on a portrait cell) and "headline" (the map
// square, a legend of the three stipples, and a large headline with the time
// and a rain-cloud icon). sm shows the map and the headline, xs the headline.

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

function marks(d) {
  const s = Number(d.map_px) || 1;
  const c = s / 2;
  const ring = Number(d.ring_px) || 0;
  const arm = Math.max(10, s * 0.022);
  const gap = Math.max(4, s * 0.008);
  const sw = Math.max(2, s * 0.0035);
  const fs = Math.max(13, s * 0.024);
  const label = ring > fs * 2 && d.ring_km
    ? '<text class="rr-ringlabel" x="' + (c + ring * 0.7071 + fs * 0.3).toFixed(1) + '" y="' + (c - ring * 0.7071 - fs * 0.3).toFixed(1) + '" font-size="' + fs.toFixed(1) + '">' + esc(d.ring_km) + " km</text>"
    : "";
  return '<circle class="rr-ring" cx="' + c + '" cy="' + c + '" r="' + ring.toFixed(1) + '" stroke-width="' + sw.toFixed(1) + '" stroke-dasharray="' + (sw * 4).toFixed(1) + " " + (sw * 3).toFixed(1) + '"/>'
    + label
    + '<g class="rr-home" stroke-width="' + (sw * 1.4).toFixed(1) + '">'
    + '<line x1="' + (c - arm) + '" y1="' + c + '" x2="' + (c - gap) + '" y2="' + c + '"/>'
    + '<line x1="' + (c + gap) + '" y1="' + c + '" x2="' + (c + arm) + '" y2="' + c + '"/>'
    + '<line x1="' + c + '" y1="' + (c - arm) + '" x2="' + c + '" y2="' + (c - gap) + '"/>'
    + '<line x1="' + c + '" y1="' + (c + gap) + '" x2="' + c + '" y2="' + (c + arm) + '"/>'
    + '<circle cx="' + c + '" cy="' + c + '" r="' + (gap * 0.9).toFixed(1) + '"/>'
    + "</g>";
}

// One SVG carries the whole picture: the Natural Earth coast as paths
// painted from the theme's tokens, the radar PNG (transparent where it is
// dry) as an image in the same pixel space, and for the main map the ring
// and the home mark. The viewBox is the server's map square, so every
// layer lands on the same pixels whatever shape the cell is.
function mapSvg(d, src, coastPx, withMarks) {
  const s = Number(d.map_px) || 1;
  return '<svg class="rr-mapsvg" viewBox="0 0 ' + s + " " + s + '" preserveAspectRatio="xMidYMid slice" aria-hidden="true">'
    + '<rect class="rr-water" x="0" y="0" width="' + s + '" height="' + s + '"/>'
    + (d.land ? '<path class="rr-land" d="' + esc(d.land) + '" stroke-width="' + coastPx.toFixed(2) + '"/>' : "")
    + (d.lakes ? '<path class="rr-lake" d="' + esc(d.lakes) + '" stroke-width="' + (coastPx * 0.8).toFixed(2) + '"/>' : "")
    + (src ? '<image href="' + esc(src) + '" x="0" y="0" width="' + s + '" height="' + s + '" preserveAspectRatio="none"/>' : "")
    + (withMarks ? marks(d) : "")
    + "</svg>";
}

function mapBox(d) {
  const s = Number(d.map_px) || 1;
  return '<div class="rr-map">' + mapSvg(d, d.map, Math.max(1.5, s * 0.003), true) + "</div>";
}

function film(d) {
  const frames = Array.isArray(d.frames) ? d.frames : [];
  if (!frames.length) return "";
  const s = Number(d.map_px) || 1;
  return '<div class="rr-film">' + frames.map((f) =>
    '<div class="rr-frame"><div class="rr-thumb">' + mapSvg(d, f.thumb, s / 110, false) + "</div>"
    + '<div class="rr-ftime">' + esc(f.time) + "</div></div>").join("") + "</div>";
}

function legend(d) {
  const c = d.colours || {};
  const light = esc(c.light || "#2a7f86");
  const heavy = esc(c.heavy || "#1b3f45");
  return '<div class="rr-legend">'
    + '<span class="rr-sw rr-sw-light" style="--rr-c:' + light + '"></span><span>light</span>'
    + '<span class="rr-dot">&middot;</span>'
    + '<span class="rr-sw rr-sw-mod" style="--rr-c:' + light + '"></span><span>moderate</span>'
    + '<span class="rr-dot">&middot;</span>'
    + '<span class="rr-sw rr-sw-heavy" style="--rr-c:' + heavy + '"></span><span>heavy</span>'
    + "</div>";
}

function titleText(d) {
  const t = d.title == null ? "Rain radar" : String(d.title).trim();
  return t || d.label || "Rain radar";
}

function credits(d) {
  const bits = [esc(d.credits || "Radar: RainViewer")];
  if (d.forecast_source === "drift") bits.push("Next hour from drift");
  else if (d.forecast_source === "persistence") bits.push("Next hour assumes no drift");
  const notice = Array.isArray(d.notices) && d.notices.length ? '<span class="rr-notice">' + esc(d.notices[0]) + "</span>" : "";
  return '<div class="rr-credit">' + notice + bits.join(" &middot; ") + "</div>";
}

function stripLayout(d) {
  return '<div class="rr-strip-main">' + mapBox(d) + film(d) + "</div>"
    + '<div class="rr-side"><div class="rr-rule"></div><div class="rr-vert">'
    + (d.sub ? '<div class="rr-sub">' + esc(d.sub) + "</div>" : "")
    + '<div class="rr-head">' + esc(d.headline) + "</div>"
    + '<div class="rr-title">' + esc(titleText(d)) + ' <span class="rr-sep">&middot;</span> <span class="rr-time">' + esc(d.latest_time) + "</span></div>"
    + "</div></div>"
    + credits(d);
}

function headlineBlock(d, withIcon) {
  const long = String(d.headline || "").length > 22 ? " is-long" : "";
  return '<div class="rr-hblock">'
    + '<div class="rr-hrow"><div class="rr-htext"><div class="rr-head' + long + '">' + esc(d.headline) + "</div>"
    + '<div class="rr-hsub"><span class="rr-time">' + esc(d.latest_time) + "</span>"
    + (d.sub ? ' <span class="rr-sep">&middot;</span> ' + esc(d.sub) : "") + "</div></div>"
    + (withIcon ? '<i class="ph-bold ph-cloud-rain rr-icon" aria-hidden="true"></i>' : "")
    + "</div></div>";
}

function headlineLayout(d, small) {
  return '<div class="rr-hl-map">' + mapBox(d) + "</div>"
    + '<div class="rr-hl-side">'
    + '<div class="rr-eyebrow">' + esc(titleText(d)) + (d.label && d.label !== titleText(d) ? ' <span class="rr-sep">&middot;</span> ' + esc(d.label) : "") + "</div>"
    + (small ? "" : legend(d))
    + headlineBlock(d, true)
    + (small ? "" : credits(d))
    + "</div>";
}

function xsLayout(d) {
  return '<div class="rr-xs">'
    + '<i class="ph-bold ph-cloud-rain rr-icon" aria-hidden="true"></i>'
    + '<div class="rr-head">' + esc(d.headline) + "</div>"
    + '<div class="rr-hsub">' + esc(d.latest_time) + "</div>"
    + "</div>";
}

function errorCard(message) {
  return '<div class="rr-err"><i class="ph-bold ph-warning-circle" aria-hidden="true"></i>'
    + '<div><div class="rr-err-t">Rain radar</div><div class="rr-err-b">' + esc(message) + "</div></div></div>";
}

export default async function render(shadow, ctx) {
  const d = (ctx && ctx.data) || {};
  const cell = (ctx && ctx.cell) || {};
  const size = cell.size || "md";
  const w = Number(cell.w) || 640;
  const h = Number(cell.h) || 400;
  const portrait = h > w * 1.08;
  const layout = d.layout === "headline" ? "headline" : "strip";
  const status = esc(d.status || "dry");

  let body;
  let mode;
  if (d.error || !d.map) {
    mode = "err";
    body = errorCard(d.error || "No radar picture yet.");
  } else if (size === "xs") {
    mode = "xs";
    body = xsLayout(d);
  } else if (size === "sm") {
    mode = "headline small";
    body = headlineLayout(d, true);
  } else if (layout === "headline") {
    mode = "headline";
    body = headlineLayout(d, false);
  } else {
    mode = "strip";
    body = stripLayout(d);
  }

  shadow.innerHTML = '<link rel="stylesheet" href="/static/style/spectra-widgets.css">'
    + '<link rel="stylesheet" href="/plugins/rain_radar/client.css">'
    + '<div class="rr size-' + esc(size) + " rr-" + mode + (portrait ? " is-portrait" : " is-landscape")
    + ' is-' + status + '" data-widget="rain_radar">' + body + "</div>";
}
