//! Minimal geometry types. Serde form: a point is `[x, y]`, a line `[[x, y], ...]`,
//! a multipolygon `[[ring, ...], ...]` (each ring a list of `[x, y]`).
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Default, Serialize, Deserialize)]
#[serde(from = "[f64; 2]", into = "[f64; 2]")]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

impl From<[f64; 2]> for Point {
    fn from(a: [f64; 2]) -> Self {
        Point { x: a[0], y: a[1] }
    }
}

impl From<Point> for [f64; 2] {
    fn from(p: Point) -> Self {
        [p.x, p.y]
    }
}

impl Point {
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }
    pub fn distance(&self, o: &Point) -> f64 {
        ((self.x - o.x).powi(2) + (self.y - o.y).powi(2)).sqrt()
    }
}

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct LineString(pub Vec<Point>);

impl LineString {
    /// Planar length.
    pub fn length(&self) -> f64 {
        self.0.windows(2).map(|w| w[0].distance(&w[1])).sum()
    }
    pub fn first(&self) -> Option<&Point> {
        self.0.first()
    }
    pub fn last(&self) -> Option<&Point> {
        self.0.last()
    }
    /// Point half way along the line (the first vertex for a zero-length line).
    pub fn midpoint(&self) -> Option<Point> {
        let half = self.length() / 2.0;
        let mut run = 0.0;
        for w in self.0.windows(2) {
            let seg = w[0].distance(&w[1]);
            if seg > 0.0 && run + seg >= half {
                let t = (half - run) / seg;
                return Some(Point::new(
                    w[0].x + t * (w[1].x - w[0].x),
                    w[0].y + t * (w[1].y - w[0].y),
                ));
            }
            run += seg;
        }
        self.0.first().copied()
    }
}

/// Polygons -> rings (first is exterior, rest are holes) -> points.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct MultiPolygon(pub Vec<Vec<Vec<Point>>>);

fn ring_area(r: &[Point]) -> f64 {
    if r.len() < 3 {
        return 0.0;
    }
    let mut s = 0.0;
    for i in 0..r.len() {
        let a = r[i];
        let b = r[(i + 1) % r.len()];
        s += a.x * b.y - b.x * a.y;
    }
    (s / 2.0).abs()
}

impl MultiPolygon {
    /// Centre of the bounding box (a label point for issues; not a true centroid).
    pub fn centre(&self) -> Option<Point> {
        bbox(self.0.iter().flatten().flatten())
            .map(|(a, b, c, d)| Point::new((a + c) / 2.0, (b + d) / 2.0))
    }
    /// Planar area (shoelace); holes are subtracted.
    pub fn area(&self) -> f64 {
        self.0
            .iter()
            .map(|poly| {
                let mut it = poly.iter();
                let ext = it.next().map(|r| ring_area(r)).unwrap_or(0.0);
                ext - it.map(|r| ring_area(r)).sum::<f64>()
            })
            .sum()
    }
}

/// Bounding box as (minx, miny, maxx, maxy).
pub type BBox = (f64, f64, f64, f64);

pub fn bbox<'a>(pts: impl IntoIterator<Item = &'a Point>) -> Option<BBox> {
    let mut b: Option<BBox> = None;
    for p in pts {
        b = Some(match b {
            None => (p.x, p.y, p.x, p.y),
            Some((a, bb, c, d)) => (a.min(p.x), bb.min(p.y), c.max(p.x), d.max(p.y)),
        });
    }
    b
}
