import { Alert, Card, Col, Empty, Row } from "antd";
import ScanForm from "../scan/ScanForm";
import ScanResults from "../scan/ScanResults";
import { useScan } from "../scan/useScan";

const MUTED = "#8b98b5";

/** Find symbol + strategy combinations worth paper trading: practice on older years, exam on the latest one. */
export default function ScanPage() {
  const { scan, scans, error, starting, start, stop, show } = useScan();
  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} md={7} lg={6}>
        <ScanForm onRun={start} starting={starting} />
      </Col>
      <Col xs={24} md={17} lg={18}>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} closable />}
        {scan ? (
          <ScanResults scan={scan} scans={scans} onPick={show} onStop={stop} />
        ) : (
          <Card>
            <Empty
              description={
                <span style={{ color: MUTED }}>
                  Pick symbols and strategies. The scan tries every combination on the practice years, then sits the ones
                  that passed on the exam year they never saw. Only what passes both is a real candidate.
                </span>
              }
            />
          </Card>
        )}
      </Col>
    </Row>
  );
}
