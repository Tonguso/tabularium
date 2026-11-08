package net.tabularium.agents;
public final class OriginPolicyTest {
    public static void main(String[] args) throws Exception {
        String base = "https://your-pc.your-tailnet.ts.net";
        if (!OriginPolicy.allows(base, base + "/#messaging")) throw new AssertionError("same origin denied");
        for (String outside : new String[]{"https://example.com", "https://ts.net", "http://your-pc.your-tailnet.ts.net"}) {
            if (OriginPolicy.origin(outside) != null) throw new AssertionError("non-tailnet configuration accepted");
        }
        for (String bad : new String[]{"http://your-pc.your-tailnet.ts.net", "https://other.your-tailnet.ts.net",
            "https://your-pc.your-tailnet.ts.net.evil.test", new java.net.URI("https", "evil", "your-pc.your-tailnet.ts.net", -1, "/", null, null).toString(),
            "file:///sdcard/private", "javascript:alert(1)", "https://your-pc.your-tailnet.ts.net:444/"}) {
            if (OriginPolicy.allows(base, bad)) throw new AssertionError("unsafe origin accepted: " + bad);
        }
        System.out.println("Origin policy checks passed");
    }
}
