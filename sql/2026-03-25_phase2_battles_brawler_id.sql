-- Phase 2: store stable brawler_id on battles

ALTER TABLE public.battles
  ADD COLUMN IF NOT EXISTS brawler_id integer;

CREATE INDEX IF NOT EXISTS idx_battles_brawler_id
  ON public.battles(brawler_id);

-- Best-effort backfill from existing brawler_name -> brawlers.id mapping
UPDATE public.battles AS bt
SET brawler_id = br.id
FROM public.brawlers AS br
WHERE bt.brawler_id IS NULL
  AND bt.brawler_name IS NOT NULL
  AND lower(br.name) = lower(bt.brawler_name);
