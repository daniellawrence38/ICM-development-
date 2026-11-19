//! Minimal geometry types.

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

impl Point {
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }
    pub fn distance(&self, o: &Point) -> f64 {
        ((self.x - o.x).powi(2) + (self.y - o.y).powi(2)).sqrt()
    }
}

#[derive(Debug, Clone, PartialEq, Default)]
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
}

/// Polygons -> rings (first is exterior, rest are holes) -> points.
#[derive(Debug, Clone, PartialEq, Default)]
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
