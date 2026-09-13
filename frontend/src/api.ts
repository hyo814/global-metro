export type Stop = {
  stop_name: string;
  lat: string;
  lon: string;
  stop_name_ko: string;
  feed_id: string;
  stop_id: string;
  country: string;
  agency: string;
  agency_ko: string;
  is_station: boolean;
  trips: number;
};

export type Run = {
  time: string;
  in_min: number;
  day_offset: number;
  route: string;
  route_ko: string;
  headsign: string;
  headsign_ko: string;
  color: string | null;
  text_color: string;
};

export type Board = {
  agency: string;
  timezone: string;
  country: string;
  local_time: string;
  departures: Run[];
  error?: string;
};

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json() as Promise<T>;
}

export const countries = () => get<{ code: string; feeds: number }[]>("/api/countries");

export const searchStops = (q: string, country: string, near?: Stop | null) =>
  get<Stop[]>(
    `/api/stops?q=${encodeURIComponent(q)}&country=${encodeURIComponent(country)}` +
      (near ? `&near=${encodeURIComponent(`${near.lat},${near.lon}`)}` : ""),
  );

export type Leg = {
  mode: "walk" | "ride";
  from: string; from_ko: string;
  to: string; to_ko: string;
  minutes: number;
  route?: string;
  headsign?: string; headsign_ko?: string;
  depart?: string; arrive?: string;
  stops?: number;
};

export type Plan = {
  legs: Leg[];
  transfers: number;
  depart: string;
  arrive: string;
  minutes: number;
  max_wait: number;
  walk_minutes: number;
  board: string;
};

export type Route = {
  agency: string; timezone: string; local_time: string;
  feeds: number; walk: number;
  plans: Plan[];
  error?: string;
};

export const stopById = (feedId: string, stopId: string) =>
  get<Stop>(
    `/api/stop?feed_id=${encodeURIComponent(feedId)}&stop_id=${encodeURIComponent(stopId)}`,
  );

export const findRoute = (from: Stop, to: Stop, walk: number) =>
  get<Route>(
    `/api/route?from_feed=${encodeURIComponent(from.feed_id)}` +
      `&from_stop=${encodeURIComponent(from.stop_id)}` +
      `&to_feed=${encodeURIComponent(to.feed_id)}` +
      `&to_stop=${encodeURIComponent(to.stop_id)}&walk=${walk}`,
  );

export const board = (feedId: string, stopId: string) =>
  get<Board>(
    `/api/departures?feed_id=${encodeURIComponent(feedId)}&stop_id=${encodeURIComponent(stopId)}`,
  );

/** 번역은 한 번에 5초쯤 걸린다. 화면을 먼저 그리고 뒤에서 채운다. */
export async function translate(texts: string[]): Promise<Record<string, string>> {
  const want = [...new Set(texts.filter(Boolean))];
  if (!want.length) return {};
  try {
    const r = await fetch("/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texts: want }),
    });
    return r.ok ? await r.json() : {};
  } catch {
    return {}; // 번역이 안 돼도 원어로 쓸 수 있다
  }
}
