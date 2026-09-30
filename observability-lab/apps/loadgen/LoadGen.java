import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Synthetic PetClinic users. Runs under the OpenTelemetry Java agent, so every call made with
 * java.net.http.HttpClient becomes an HTTP CLIENT span and the W3C traceparent header is injected
 * - PetClinic's SERVER span joins the same trace: loadgen -> petclinic -> postgresql.
 *
 * Tunables (env): TARGET, RPS, CONCURRENCY, ERROR_PCT (extra share of failing requests, 0-100).
 * Each request carries "X-Lab-Scenario: <name>" (captured by PetClinic's agent as a span attribute).
 */
public class LoadGen {

	private static final Logger log = Logger.getLogger("petclinic.loadgen");

	// owner id -> pet ids, from PetClinic's sample data (db/postgres/data.sql)
	private static final Map<Integer, int[]> PETS = Map.of(1, new int[] { 1 }, 2, new int[] { 2 }, 3,
			new int[] { 3, 4 }, 4, new int[] { 5 }, 5, new int[] { 6 }, 6, new int[] { 7, 8 }, 7, new int[] { 9 }, 8,
			new int[] { 10 }, 9, new int[] { 11 }, 10, new int[] { 12, 13 });

	private static final List<String> LAST_NAMES = List.of("", "Davis", "Franklin", "Black", "Coleman", "Escobito",
			"Schroeder", "McTavish", "Nobody");

	record Scenario(String name, int weight, boolean failing) {
	}

	private static final List<Scenario> NORMAL = List.of(new Scenario("home", 8, false),
			new Scenario("vets-page", 10, false), new Scenario("vets-api", 5, false),
			new Scenario("find-owners", 20, false), new Scenario("owner-details", 25, false),
			new Scenario("edit-owner-form", 5, false), new Scenario("add-visit", 6, false),
			new Scenario("new-owner", 1, false), new Scenario("invalid-owner-form", 3, false));

	private static final List<Scenario> FAILING = List.of(new Scenario("error-oups", 4, true),
			new Scenario("error-owner-not-found", 4, true), new Scenario("http-404", 2, true));

	private final HttpClient http = HttpClient.newBuilder()
		.connectTimeout(Duration.ofSeconds(3))
		.followRedirects(HttpClient.Redirect.NORMAL)
		.build();

	private final String target;

	LoadGen(String target) {
		this.target = target;
	}

	public static void main(String[] args) {
		String target = env("TARGET", "http://petclinic.petclinic.svc.cluster.local");
		double rps = Double.parseDouble(env("RPS", "4"));
		int concurrency = Integer.parseInt(env("CONCURRENCY", "4"));
		int errorPct = Integer.parseInt(env("ERROR_PCT", "5"));
		log.info(() -> String.format("loadgen starting target=%s rps=%.1f concurrency=%d error_pct=%d", target, rps,
				concurrency, errorPct));

		LoadGen gen = new LoadGen(target);
		var workers = Executors.newFixedThreadPool(concurrency);
		var ticker = Executors.newSingleThreadScheduledExecutor();
		long periodMicros = (long) (1_000_000 / rps);
		ticker.scheduleAtFixedRate(() -> workers.submit(() -> gen.runOne(errorPct)), 0, periodMicros,
				TimeUnit.MICROSECONDS);
	}

	void runOne(int errorPct) {
		ThreadLocalRandom rnd = ThreadLocalRandom.current();
		Scenario s = pick(rnd.nextInt(100) < errorPct ? FAILING : NORMAL, rnd);
		try {
			HttpResponse<Void> res = http.send(request(s, rnd), HttpResponse.BodyHandlers.discarding());
			int code = res.statusCode();
			if (code >= 500) {
				log.warning(() -> "scenario=" + s.name() + " status=" + code + " uri=" + res.uri());
			}
			else if (code >= 400) {
				log.info(() -> "scenario=" + s.name() + " status=" + code + " uri=" + res.uri());
			}
		}
		catch (Exception e) {
			log.log(Level.SEVERE, "scenario=" + s.name() + " request failed: " + e, e);
		}
	}

	HttpRequest request(Scenario s, ThreadLocalRandom rnd) {
		int owner = rnd.nextInt(1, 11);
		int[] pets = PETS.get(owner);
		int pet = pets[rnd.nextInt(pets.length)];
		return switch (s.name()) {
			case "home" -> get(s, "/");
			case "vets-page" -> get(s, "/vets.html?page=" + rnd.nextInt(1, 3));
			case "vets-api" -> get(s, "/vets");
			case "find-owners" -> get(s, "/owners?lastName=" + enc(LAST_NAMES.get(rnd.nextInt(LAST_NAMES.size()))));
			case "owner-details" -> get(s, "/owners/" + owner);
			case "edit-owner-form" -> get(s, "/owners/" + owner + "/edit");
			case "add-visit" -> post(s, "/owners/" + owner + "/pets/" + pet + "/visits/new",
					"date=" + LocalDate.now() + "&description=" + enc("checkup #" + rnd.nextInt(10_000)));
			case "new-owner" -> post(s, "/owners/new", "firstName=Lab&lastName=" + enc("User" + rnd.nextInt(100_000))
					+ "&address=" + enc("1 Trace St.") + "&city=Otel&telephone=" + (6_000_000_000L + rnd.nextInt(1_000_000)));
			// validation error: telephone must be 10 digits -> form re-rendered (HTTP 200, no exception)
			case "invalid-owner-form" -> post(s, "/owners/new",
					"firstName=Bad&lastName=Input&address=x&city=y&telephone=abc");
			// CrashController throws RuntimeException -> HTTP 500 + exception event on the span
			case "error-oups" -> get(s, "/oups");
			// OwnerController.orElseThrow(IllegalArgumentException) -> HTTP 500
			case "error-owner-not-found" -> get(s, "/owners/" + rnd.nextInt(90_000, 99_999));
			case "http-404" -> get(s, "/no-such-page-" + rnd.nextInt(5));
			default -> throw new IllegalStateException(s.name());
		};
	}

	HttpRequest get(Scenario s, String path) {
		return base(s, path).GET().build();
	}

	HttpRequest post(Scenario s, String path, String form) {
		return base(s, path).header("Content-Type", "application/x-www-form-urlencoded")
			.POST(HttpRequest.BodyPublishers.ofString(form))
			.build();
	}

	HttpRequest.Builder base(Scenario s, String path) {
		return HttpRequest.newBuilder(URI.create(target + path))
			.timeout(Duration.ofSeconds(20))
			.header("X-Lab-Scenario", s.name())
			.header("User-Agent", "petclinic-loadgen/1.0");
	}

	static Scenario pick(List<Scenario> list, ThreadLocalRandom rnd) {
		int total = list.stream().mapToInt(Scenario::weight).sum();
		int r = rnd.nextInt(total);
		for (Scenario s : list) {
			r -= s.weight();
			if (r < 0) {
				return s;
			}
		}
		return list.get(0);
	}

	static String enc(String v) {
		return URLEncoder.encode(v, StandardCharsets.UTF_8);
	}

	static String env(String k, String def) {
		String v = System.getenv(k);
		return v == null || v.isBlank() ? def : v;
	}

}
