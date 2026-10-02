/** How a place's category and rating are shown — on the day cards and in the map popup. */

import {
  Camera,
  Castle,
  Drama,
  Flower2,
  Landmark,
  Leaf,
  MapPin,
  Martini,
  Mountain,
  ShoppingBag,
  Trees,
  Trophy,
  Utensils,
  Waves,
  type LucideIcon,
} from "lucide-react";

export interface CategoryMeta {
  label: string;
  icon: LucideIcon;
}

// Keys are the categories the attractions tool returns (src/ai/mcp_server/tools.py).
const CATEGORIES: Record<string, CategoryMeta> = {
  beach:       { label: "Beach",          icon: Waves },
  nature:      { label: "Nature",         icon: Trees },
  history:     { label: "History",        icon: Castle },
  spiritual:   { label: "Spiritual site", icon: Flower2 },
  museum:      { label: "Museum",         icon: Landmark },
  culture:     { label: "Culture",        icon: Drama },
  food:        { label: "Food",           icon: Utensils },
  nightlife:   { label: "Nightlife",      icon: Martini },
  shopping:    { label: "Shopping",       icon: ShoppingBag },
  wellness:    { label: "Wellness",       icon: Leaf },
  adventure:   { label: "Adventure",      icon: Mountain },
  sports:      { label: "Sports",         icon: Trophy },
  sightseeing: { label: "Sightseeing",    icon: Camera },
};

export function categoryMeta(category?: string | null): CategoryMeta | null {
  if (!category) return null;
  return CATEGORIES[category] ?? { label: category.charAt(0).toUpperCase() + category.slice(1), icon: MapPin };
}

export interface PlaceRating {
  /** 1 = worth a stop … 3 = top attraction */
  level: 1 | 2 | 3;
  label: string;
  /** listed as cultural heritage */
  heritage: boolean;
}

const RATING_LABELS = ["Worth a stop", "Popular", "Top attraction"] as const;

/**
 * The attraction rating is OpenTripMap's popularity rate, not a star score:
 * 1–3, or 5–7 for the same scale on a cultural-heritage site. 0 or missing
 * means unrated, and then nothing is shown.
 */
export function describeRating(rating?: number | null): PlaceRating | null {
  if (!rating || rating < 1) return null;
  const heritage = rating > 3;
  const level = Math.min(3, Math.max(1, Math.round(heritage ? rating - 4 : rating))) as 1 | 2 | 3;
  return { level, label: RATING_LABELS[level - 1], heritage };
}
