-- Empty-database baseline: schema only, no player/account records. PostgreSQL 17.
--
-- PostgreSQL database dump
--


-- Dumped from database version 17.10
-- Dumped by pg_dump version 17.10




--
-- Name: battle_features_3v3; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.battle_features_3v3 (
    battletime timestamp with time zone NOT NULL,
    mode text NOT NULL,
    map_key text NOT NULL,
    type text NOT NULL,
    duration integer NOT NULL,
    player_tag text NOT NULL,
    map_id bigint,
    map text,
    player_name text,
    brawler_name text,
    win_label smallint,
    player_total_trophies integer,
    player_level integer,
    brawler_power integer,
    brawler_total_trophies integer,
    brawler_rank_on_account integer,
    is_starplayer boolean,
    is_highest_player_brawler boolean,
    rank integer,
    trophychange integer,
    team_size integer,
    team_avg_player_total_trophies double precision,
    team_std_player_total_trophies double precision,
    team_max_player_total_trophies double precision,
    team_avg_player_level double precision,
    team_min_player_level integer,
    team_max_player_level integer,
    team_std_player_level double precision,
    team_avg_brawler_power double precision,
    team_min_brawler_power integer,
    team_max_brawler_power integer,
    team_avg_brawler_total_trophies double precision,
    team_min_brawler_total_trophies integer,
    team_max_brawler_total_trophies integer,
    team_std_brawler_total_trophies double precision,
    team_avg_brawler_rank_on_account double precision,
    team_min_brawler_rank_on_account integer,
    team_max_brawler_rank_on_account integer,
    team_std_brawler_rank_on_account double precision,
    team_pct_highest_player_brawler double precision,
    team_comp_key text,
    enemy_size integer,
    enemy_avg_player_total_trophies double precision,
    enemy_std_player_total_trophies double precision,
    enemy_max_player_total_trophies double precision,
    enemy_avg_player_level double precision,
    enemy_min_player_level integer,
    enemy_max_player_level integer,
    enemy_std_player_level double precision,
    enemy_avg_brawler_power double precision,
    enemy_min_brawler_power integer,
    enemy_max_brawler_power integer,
    enemy_avg_brawler_total_trophies double precision,
    enemy_min_brawler_total_trophies integer,
    enemy_max_brawler_total_trophies integer,
    enemy_std_brawler_total_trophies double precision,
    enemy_avg_brawler_rank_on_account double precision,
    enemy_min_brawler_rank_on_account integer,
    enemy_max_brawler_rank_on_account integer,
    enemy_std_brawler_rank_on_account double precision,
    enemy_pct_highest_player_brawler double precision,
    enemy_comp_key text,
    matchup_key text,
    diff_avg_player_total_trophies double precision,
    diff_std_player_total_trophies double precision,
    diff_avg_player_level double precision,
    diff_min_player_level double precision,
    diff_max_player_level double precision,
    diff_std_player_level double precision,
    diff_avg_brawler_power double precision,
    diff_min_brawler_power double precision,
    diff_max_brawler_power double precision,
    diff_avg_brawler_total_trophies double precision,
    diff_min_brawler_total_trophies double precision,
    diff_max_brawler_total_trophies double precision,
    diff_std_brawler_total_trophies double precision,
    diff_avg_brawler_rank_on_account double precision,
    diff_min_brawler_rank_on_account double precision,
    diff_max_brawler_rank_on_account double precision,
    diff_std_brawler_rank_on_account double precision,
    diff_pct_highest_player_brawler double precision,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    pred_win_prob double precision,
    model_version text,
    pred_updated_at timestamp with time zone,
    pred_explanation_tech text,
    pred_explanation_player text
);


--
-- Name: battles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.battles (
    battletime text NOT NULL,
    mode text,
    map text,
    result text,
    type text,
    duration integer,
    player_tag text NOT NULL,
    player_name text,
    player_total_trophies integer,
    brawler_name text,
    brawler_power integer,
    brawler_trophies integer,
    brawler_total_trophies integer,
    brawler_rank_on_account integer,
    is_starplayer boolean,
    rank integer,
    trophychange integer,
    is_highest_player_brawler boolean,
    player_level integer,
    map_id bigint,
    battletime_ts timestamp with time zone,
    brawler_id integer
);


--
-- Name: brawlers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brawlers (
    id integer NOT NULL,
    name text NOT NULL,
    class text,
    rarity text,
    borderless_url text,
    border_url text,
    emoji_url text,
    model_url text,
    portrait_url text
);


--
-- Name: club_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.club_memberships (
    club_tag text NOT NULL,
    player_tag text NOT NULL,
    role text,
    trophies integer,
    name text,
    name_color text,
    icon_id integer,
    is_active boolean DEFAULT true NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: clubs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clubs (
    tag text NOT NULL,
    name text,
    description text,
    type text,
    badge_id integer,
    required_trophies integer,
    trophies integer,
    member_count integer,
    last_update timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: game_modes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.game_modes (
    id integer NOT NULL,
    sc_id integer,
    name text NOT NULL,
    hash text,
    sc_hash text,
    regular_url text
);


--
-- Name: league_victory_baselines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.league_victory_baselines (
    window_days integer NOT NULL,
    mode text NOT NULL,
    type text NOT NULL,
    brawler_name text NOT NULL,
    n_battles integer NOT NULL,
    league_win_rate double precision NOT NULL,
    league_avg_difficulty double precision NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    predicted_wins integer DEFAULT 0 NOT NULL,
    surprise_wins integer DEFAULT 0 NOT NULL,
    chokes integer DEFAULT 0 NOT NULL
);


--
-- Name: maps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.maps (
    id integer NOT NULL,
    name text NOT NULL,
    hash text,
    link text,
    image_url text,
    environment_id integer,
    environment_sc_id integer,
    environment_name text,
    environment_hash text,
    environment_image_url text,
    game_mode_id integer,
    game_mode_sc_id integer,
    game_mode_name text,
    game_mode_hash text,
    game_mode_image_url text,
    last_active timestamp with time zone,
    data_updated bigint
);


--
-- Name: owner_preferences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_preferences (
    owner_tag text NOT NULL,
    selected_list_id bigint,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: player_brawler_daily_activity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.player_brawler_daily_activity (
    player_tag text NOT NULL,
    brawler_name text NOT NULL,
    activity_date date NOT NULL,
    trophies integer,
    games integer DEFAULT 0,
    wins integer DEFAULT 0,
    losses integer DEFAULT 0,
    draws integer DEFAULT 0,
    last_update timestamp with time zone DEFAULT now()
);


--
-- Name: player_brawlers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.player_brawlers (
    player_tag text NOT NULL,
    brawler_id integer NOT NULL,
    brawler_name text,
    power integer,
    rank integer,
    trophies integer,
    highest_trophies integer,
    star_powers_json jsonb,
    gadgets_json jsonb,
    gears_json jsonb,
    last_update timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: player_daily_activity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.player_daily_activity (
    activity_date date NOT NULL,
    player_tag text NOT NULL,
    trophies integer,
    highest_trophies integer,
    exp_level integer,
    brawler_count integer,
    total_brawler_trophies integer,
    victories_3v3 integer,
    victories_solo integer,
    victories_duo integer,
    last_update timestamp with time zone DEFAULT now()
);


--
-- Name: player_list_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.player_list_members (
    list_id bigint NOT NULL,
    player_tag text NOT NULL,
    is_removed boolean DEFAULT false NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    removed_at timestamp with time zone
);


--
-- Name: player_lists; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.player_lists (
    id bigint NOT NULL,
    owner_tag text NOT NULL,
    name text NOT NULL,
    is_favorite boolean DEFAULT false NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_system boolean DEFAULT false NOT NULL,
    system_key text,
    is_deletable boolean DEFAULT true NOT NULL,
    is_public boolean DEFAULT false NOT NULL,
    share_token text
);


--
-- Name: player_lists_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.player_lists_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: player_lists_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.player_lists_id_seq OWNED BY public.player_lists.id;


--
-- Name: player_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.player_profiles (
    tag text NOT NULL,
    name text NOT NULL,
    icon_id integer,
    trophies integer,
    highest_trophies integer,
    exp_level integer,
    "3v3_victories" integer,
    solo_victories integer,
    duo_victories integer,
    favorite_brawler_name text,
    favorite_brawler_trophies integer,
    favorite_brawler_rank_on_account integer,
    first_seen timestamp with time zone DEFAULT now(),
    last_update timestamp with time zone DEFAULT now(),
    club_tag text,
    club_name text,
    name_color text,
    exp_points integer,
    power_play_points integer,
    highest_power_play_points integer,
    is_qualified_from_championship_challenge boolean,
    best_robo_rumble_time integer,
    best_time_as_big_brawler integer
);


--
-- Name: player_victory_rollups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.player_victory_rollups (
    player_tag text NOT NULL,
    window_days integer NOT NULL,
    mode text NOT NULL,
    type text NOT NULL,
    brawler_name text NOT NULL,
    n_battles integer NOT NULL,
    wins integer NOT NULL,
    losses integer NOT NULL,
    xw double precision NOT NULL,
    win_rate double precision NOT NULL,
    overperf double precision NOT NULL,
    avg_difficulty double precision NOT NULL,
    league_win_rate double precision NOT NULL,
    league_avg_difficulty double precision NOT NULL,
    skill_diff double precision NOT NULL,
    win_rate_score double precision NOT NULL,
    overperf_score double precision NOT NULL,
    difficulty_score double precision NOT NULL,
    victory_score double precision NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    predicted_wins integer,
    surprise_wins integer,
    chokes integer,
    league_predicted_wins integer DEFAULT 0 NOT NULL,
    league_surprise_wins integer DEFAULT 0 NOT NULL,
    league_chokes integer DEFAULT 0 NOT NULL,
    pred_threshold double precision
);


--
-- Name: player_lists id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_lists ALTER COLUMN id SET DEFAULT nextval('public.player_lists_id_seq'::regclass);


--
-- Name: battle_features_3v3 battle_features_3v3_pk; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.battle_features_3v3
    ADD CONSTRAINT battle_features_3v3_pk PRIMARY KEY (battletime, mode, map_key, type, duration, player_tag);


--
-- Name: battles battles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.battles
    ADD CONSTRAINT battles_pkey PRIMARY KEY (battletime, player_tag);


--
-- Name: brawlers brawlers_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brawlers
    ADD CONSTRAINT brawlers_name_key UNIQUE (name);


--
-- Name: brawlers brawlers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brawlers
    ADD CONSTRAINT brawlers_pkey PRIMARY KEY (id);


--
-- Name: club_memberships club_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_memberships
    ADD CONSTRAINT club_memberships_pkey PRIMARY KEY (club_tag, player_tag);


--
-- Name: clubs clubs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clubs
    ADD CONSTRAINT clubs_pkey PRIMARY KEY (tag);


--
-- Name: game_modes game_modes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.game_modes
    ADD CONSTRAINT game_modes_pkey PRIMARY KEY (id);


--
-- Name: game_modes game_modes_sc_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.game_modes
    ADD CONSTRAINT game_modes_sc_id_key UNIQUE (sc_id);


--
-- Name: league_victory_baselines league_victory_baselines_chokes_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.league_victory_baselines
    ADD CONSTRAINT league_victory_baselines_chokes_nonneg CHECK ((chokes >= 0)) NOT VALID;


--
-- Name: league_victory_baselines league_victory_baselines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_victory_baselines
    ADD CONSTRAINT league_victory_baselines_pkey PRIMARY KEY (window_days, mode, type, brawler_name);


--
-- Name: league_victory_baselines league_victory_baselines_predicted_wins_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.league_victory_baselines
    ADD CONSTRAINT league_victory_baselines_predicted_wins_nonneg CHECK ((predicted_wins >= 0)) NOT VALID;


--
-- Name: league_victory_baselines league_victory_baselines_surprise_wins_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.league_victory_baselines
    ADD CONSTRAINT league_victory_baselines_surprise_wins_nonneg CHECK ((surprise_wins >= 0)) NOT VALID;


--
-- Name: maps maps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.maps
    ADD CONSTRAINT maps_pkey PRIMARY KEY (id);


--
-- Name: owner_preferences owner_preferences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_preferences
    ADD CONSTRAINT owner_preferences_pkey PRIMARY KEY (owner_tag);


--
-- Name: player_brawler_daily_activity player_brawler_daily_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_brawler_daily_activity
    ADD CONSTRAINT player_brawler_daily_activity_pkey PRIMARY KEY (player_tag, brawler_name, activity_date);


--
-- Name: player_brawlers player_brawlers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_brawlers
    ADD CONSTRAINT player_brawlers_pkey PRIMARY KEY (player_tag, brawler_id);


--
-- Name: player_daily_activity player_daily_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_daily_activity
    ADD CONSTRAINT player_daily_activity_pkey PRIMARY KEY (activity_date, player_tag);


--
-- Name: player_list_members player_list_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_list_members
    ADD CONSTRAINT player_list_members_pkey PRIMARY KEY (list_id, player_tag);


--
-- Name: player_lists player_lists_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_lists
    ADD CONSTRAINT player_lists_pkey PRIMARY KEY (id);


--
-- Name: player_lists player_lists_share_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_lists
    ADD CONSTRAINT player_lists_share_token_key UNIQUE (share_token);


--
-- Name: player_profiles player_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_profiles
    ADD CONSTRAINT player_profiles_pkey PRIMARY KEY (tag);


--
-- Name: player_victory_rollups player_victory_rollups_chokes_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.player_victory_rollups
    ADD CONSTRAINT player_victory_rollups_chokes_nonneg CHECK ((chokes >= 0)) NOT VALID;


--
-- Name: player_victory_rollups player_victory_rollups_league_chokes_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.player_victory_rollups
    ADD CONSTRAINT player_victory_rollups_league_chokes_nonneg CHECK ((league_chokes >= 0)) NOT VALID;


--
-- Name: player_victory_rollups player_victory_rollups_league_predicted_wins_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.player_victory_rollups
    ADD CONSTRAINT player_victory_rollups_league_predicted_wins_nonneg CHECK ((league_predicted_wins >= 0)) NOT VALID;


--
-- Name: player_victory_rollups player_victory_rollups_league_surprise_wins_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.player_victory_rollups
    ADD CONSTRAINT player_victory_rollups_league_surprise_wins_nonneg CHECK ((league_surprise_wins >= 0)) NOT VALID;


--
-- Name: player_victory_rollups player_victory_rollups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_victory_rollups
    ADD CONSTRAINT player_victory_rollups_pkey PRIMARY KEY (player_tag, window_days, mode, type, brawler_name);


--
-- Name: player_victory_rollups player_victory_rollups_pred_threshold_range; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.player_victory_rollups
    ADD CONSTRAINT player_victory_rollups_pred_threshold_range CHECK (((pred_threshold IS NULL) OR ((pred_threshold >= (0)::double precision) AND (pred_threshold <= (1)::double precision)))) NOT VALID;


--
-- Name: player_victory_rollups player_victory_rollups_predicted_wins_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.player_victory_rollups
    ADD CONSTRAINT player_victory_rollups_predicted_wins_nonneg CHECK ((predicted_wins >= 0)) NOT VALID;


--
-- Name: player_victory_rollups player_victory_rollups_surprise_wins_nonneg; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.player_victory_rollups
    ADD CONSTRAINT player_victory_rollups_surprise_wins_nonneg CHECK ((surprise_wins >= 0)) NOT VALID;


--
-- Name: battle_features_3v3_battletime_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX battle_features_3v3_battletime_idx ON public.battle_features_3v3 USING btree (battletime DESC);


--
-- Name: battle_features_3v3_uk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX battle_features_3v3_uk ON public.battle_features_3v3 USING btree (battletime, mode, map_key, type, duration, player_tag);


--
-- Name: battles_3v3_refresh_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX battles_3v3_refresh_idx ON public.battles USING btree (battletime_ts, mode, COALESCE((map_id)::text, map), type, duration, result) WHERE ((battletime_ts IS NOT NULL) AND (mode <> ALL (ARRAY['soloShowdown'::text, 'duoShowdown'::text])) AND (result = ANY (ARRAY['victory'::text, 'defeat'::text])));


--
-- Name: battles_battletime_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX battles_battletime_ts_idx ON public.battles USING btree (battletime_ts);


--
-- Name: battles_ts_result_mode_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX battles_ts_result_mode_idx ON public.battles USING btree (battletime_ts, result, mode);


--
-- Name: idx_battle_features_battletime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_battle_features_battletime ON public.battle_features_3v3 USING btree (battletime DESC);


--
-- Name: idx_battle_features_player_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_battle_features_player_time ON public.battle_features_3v3 USING btree (player_tag, battletime DESC);


--
-- Name: idx_battles_battletime_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_battles_battletime_ts ON public.battles USING btree (battletime_ts DESC);


--
-- Name: idx_battles_brawler_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_battles_brawler_id ON public.battles USING btree (brawler_id);


--
-- Name: idx_battles_map_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_battles_map_id ON public.battles USING btree (map_id);


--
-- Name: idx_battles_ranked_player_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_battles_ranked_player_time ON public.battles USING btree (player_tag, battletime DESC) WHERE ((type = ANY (ARRAY['teamRanked'::text, 'soloRanked'::text])) AND (brawler_trophies IS NOT NULL));


--
-- Name: idx_battles_ranked_player_trophies; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_battles_ranked_player_trophies ON public.battles USING btree (player_tag, brawler_trophies) WHERE ((type = ANY (ARRAY['teamRanked'::text, 'soloRanked'::text])) AND (brawler_trophies IS NOT NULL));


--
-- Name: idx_bf3v3_matchup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bf3v3_matchup ON public.battle_features_3v3 USING btree (matchup_key);


--
-- Name: idx_bf3v3_mode_map_comp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bf3v3_mode_map_comp ON public.battle_features_3v3 USING btree (mode, map_key, team_comp_key);


--
-- Name: idx_bf3v3_player_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bf3v3_player_time ON public.battle_features_3v3 USING btree (player_tag, battletime DESC);


--
-- Name: idx_club_memberships_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_club_memberships_active ON public.club_memberships USING btree (club_tag, is_active, trophies DESC);


--
-- Name: idx_game_modes_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_game_modes_name ON public.game_modes USING btree (name);


--
-- Name: idx_list_members_list_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_list_members_list_active ON public.player_list_members USING btree (list_id) WHERE (is_removed = false);


--
-- Name: idx_owner_preferences_selected; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_owner_preferences_selected ON public.owner_preferences USING btree (owner_tag, selected_list_id);


--
-- Name: idx_pbd_player; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pbd_player ON public.player_brawler_daily_activity USING btree (player_tag);


--
-- Name: idx_pbd_player_brawler; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pbd_player_brawler ON public.player_brawler_daily_activity USING btree (player_tag, brawler_name);


--
-- Name: idx_player_brawlers_brawler_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_player_brawlers_brawler_id ON public.player_brawlers USING btree (brawler_id);


--
-- Name: idx_player_brawlers_player_tag; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_player_brawlers_player_tag ON public.player_brawlers USING btree (player_tag);


--
-- Name: idx_player_list_members_list_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_player_list_members_list_id ON public.player_list_members USING btree (list_id);


--
-- Name: idx_player_lists_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_player_lists_owner ON public.player_lists USING btree (owner_tag) WHERE (is_deleted = false);


--
-- Name: idx_player_lists_owner_fav; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_player_lists_owner_fav ON public.player_lists USING btree (owner_tag, is_favorite DESC, updated_at DESC) WHERE (is_deleted = false);


--
-- Name: idx_player_lists_share_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_player_lists_share_token ON public.player_lists USING btree (share_token);


--
-- Name: idx_player_victory_rollups_player_window; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_player_victory_rollups_player_window ON public.player_victory_rollups USING btree (player_tag, window_days);


--
-- Name: ix_league_victory_baselines_window; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_league_victory_baselines_window ON public.league_victory_baselines USING btree (window_days);


--
-- Name: ix_player_victory_rollups_player; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_player_victory_rollups_player ON public.player_victory_rollups USING btree (player_tag, window_days);


--
-- Name: uq_player_lists_owner_system_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_player_lists_owner_system_key ON public.player_lists USING btree (owner_tag, system_key) WHERE (system_key IS NOT NULL);


--
-- Name: club_memberships club_memberships_club_tag_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_memberships
    ADD CONSTRAINT club_memberships_club_tag_fkey FOREIGN KEY (club_tag) REFERENCES public.clubs(tag) ON DELETE CASCADE;


--
-- Name: club_memberships club_memberships_player_tag_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.club_memberships
    ADD CONSTRAINT club_memberships_player_tag_fkey FOREIGN KEY (player_tag) REFERENCES public.player_profiles(tag) ON DELETE CASCADE;


--
-- Name: player_brawlers player_brawlers_player_tag_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_brawlers
    ADD CONSTRAINT player_brawlers_player_tag_fkey FOREIGN KEY (player_tag) REFERENCES public.player_profiles(tag) ON DELETE CASCADE;


--
-- Name: player_list_members player_list_members_list_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.player_list_members
    ADD CONSTRAINT player_list_members_list_id_fkey FOREIGN KEY (list_id) REFERENCES public.player_lists(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


